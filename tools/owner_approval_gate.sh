#!/bin/sh
# "Does the owner's approval cover this release SHA?" — the ONE implementation of that
# question (#1056, infra2#1035).
#
# Owner instruction, 2026-10-06: only a production deployment needs the owner's approval
# and presence. Two entry points reach production, and both run this file:
#
#   * tools/cut_release.sh --prod --owner-approved-sha <sha>, and
#   * .github/workflows/deploy-release.yml, input `owner_approved_sha`, when
#     `deploy_type` is `prod`.
#
# A second copy of the rule would drift, so the rule lives here.
#
# What this proves: the caller named the exact commit it promotes, in full. It cannot
# prove WHO typed the SHA. The owner's presence is a human fact; this check makes a
# promotion impossible by accident, from an automatic path, or for a commit other than
# the one the owner approved. The owner approves a SHA, then the agent passes that same
# SHA.
#
# Usage:
#   sh tools/owner_approval_gate.sh <owner-approved-sha> <release-sha>
#
# Exit 0: the approval is a full 40-character lowercase hex SHA and equals the release
#         SHA.
# Exit 2: anything else. The reason goes to stderr. An unreadable or empty input is a
#         refusal, never a pass.
set -eu

approved="${1-}"
release="${2-}"

is_full_sha() {
    case "$1" in
        *[!0-9a-f]*) return 1 ;;
    esac
    [ "${#1}" -eq 40 ]
}

# The release SHA comes from git or from a tag, never from a person. If it is not a full
# SHA, this check cannot compare anything, so it refuses.
if ! is_full_sha "$release"; then
    echo "owner_approval_gate: the release SHA is not a 40-character lowercase hex SHA — nothing to compare the approval with" >&2
    exit 2
fi

if [ -z "$approved" ]; then
    echo "owner_approval_gate: a production promotion needs the owner's approval of the exact release SHA ($release). Pass the 40-character SHA the owner approved." >&2
    exit 2
fi

# The approval value is NOT echoed unless it is valid: it is typed by a person and
# reaches a terminal and a CI log, where a crafted value could forge a workflow command.
if ! is_full_sha "$approved"; then
    echo "owner_approval_gate: the owner approval must be exactly 40 characters from 0-9 and a-f; the value given has ${#approved} characters" >&2
    exit 2
fi

if [ "$approved" != "$release" ]; then
    echo "owner_approval_gate: the owner approved $approved, but this release is $release — the approval does not cover this commit" >&2
    exit 2
fi
