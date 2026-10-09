import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path


def setup_logger():
    logger = logging.getLogger("check_pr_ready")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter("%(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


logger = setup_logger()


def check_anti_exclusion_invariant():
    """Scans for permanent allowlists/waivers."""
    repo_root = Path(__file__).resolve().parent.parent
    allowlists = list(repo_root.rglob("*allowlist*.json"))
    if allowlists:
        logger.error(f"Anti-Exclusion Invariant violated: Found permanent allowlist files: {allowlists}")
        return False
    return True


def get_pr_data(args):
    """Retrieves PR data from the specified source."""
    if args.pr:
        try:
            result = subprocess.run(
                ["gh", "pr", "view", args.pr, "--json", "state,reviewDecision,reviews"],
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to fetch PR data via gh cli: {e.stderr}")
            sys.exit(1)
    elif args.input_file:
        with open(args.input_file, encoding="utf-8") as f:
            return json.loads(f.read())
    elif args.json:
        return json.loads(args.json)
    elif not sys.stdin.isatty():
        return json.loads(sys.stdin.read())
    else:
        logger.error("No input provided. Use --pr, --input-file, --json, or stdin.")
        sys.exit(1)


def evaluate_pr(data, ignored_reviewers=None):
    """Evaluates PR data against readiness rules."""
    if ignored_reviewers is None:
        ignored_reviewers = []

    # Sometimes data is just a list of reviews for testing purposes.
    if isinstance(data, list):
        reviews = data
        state = "OPEN"
        review_decision = "APPROVED"
    else:
        reviews = data.get("reviews", [])
        state = data.get("state", "OPEN")
        review_decision = data.get("reviewDecision", "APPROVED")

    if state != "OPEN":
        logger.error(f"PR is not open (State: {state}).")
        return False

    error_keywords = [
        "encountered an error",
        "unable to review",
        "timed out",
        "internal error",
        "error during execution",
    ]

    for review in reviews:
        author = review.get("author", {}).get("login", "unknown")
        body = review.get("body", "").lower()
        if any(keyword in body for keyword in error_keywords):
            if author in ignored_reviewers:
                logger.warning(f"Reviewer error for '{author}' explicitly bypassed via CLI flag: {review.get('body')}")
                continue
            logger.error(f"Reviewer error detected in review body: {review.get('body')}")
            return False

    if review_decision == "CHANGES_REQUESTED":
        logger.error("Review decision is CHANGES_REQUESTED.")
        return False

    # Check for undismissed requested changes.
    # In GitHub, latest review per author determines their state.
    author_states = {}
    for review in reviews:
        author = review.get("author", {}).get("login", "unknown")
        author_states[author] = review.get("state", "COMMENTED")

    for author, review_state in author_states.items():
        if review_state == "CHANGES_REQUESTED":
            logger.error(f"Reviewer {author} requested changes which are not resolved.")
            return False

    return True


def main():
    parser = argparse.ArgumentParser(description="Fail-Closed PR Review Gate & Anti-Exclusion Checker")
    parser.add_argument("--pr", help="PR number to check via gh CLI")
    parser.add_argument("--input-file", help="Path to JSON file with PR data")
    parser.add_argument("--json", help="JSON string with PR data")
    parser.add_argument(
        "--ignore-reviewer-error",
        action="append",
        default=[],
        help="Reviewer login to ignore errors for (e.g. copilot-pull-request-reviewer during platform outage)",
    )
    args = parser.parse_args()

    if not check_anti_exclusion_invariant():
        sys.exit(1)

    try:
        pr_data = get_pr_data(args)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        sys.exit(1)

    if evaluate_pr(pr_data, ignored_reviewers=args.ignore_reviewer_error):
        logger.info("PR is ready.")
        sys.exit(0)
    else:
        logger.error("PR is NOT ready.")
        sys.exit(1)


if __name__ == "__main__":
    main()
