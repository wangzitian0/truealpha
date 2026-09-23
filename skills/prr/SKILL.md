---
name: prr
description: PR Review and Response - Comprehensive PR review workflow with conflict checking, comment handling, and code review
ship: repo
---

# PR Review and Response Workflow

Execute a comprehensive PR review workflow that handles conflicts, processes review comments, performs code review, and pushes changes.

## Quick Start

When user invokes `/prr`, execute the following workflow:

1. **Detect PR Context**: Check if in git repo, identify current PR
2. **Check Conflicts**: Rebase if branch is behind base branch
3. **Fetch Comments**: Get all PR review comments and general comments
4. **Process Comments**: For each comment, determine if code change is needed
5. **Make Changes**: Implement fixes or respond to comments
6. **Resolve Comments**: Mark handled comments as resolved via GitHub API
7. **Code Review**: Perform comprehensive review of all changes
8. **Push & Report**: Push changes and display PR link(s)

**Key Commands:**
- `gh pr view` - Get PR information
- `gh api graphql` - Query/mutate GitHub data
- `git rebase` - Update branch
- `git push` - Push changes

A unified gate command that folds CI status, weighted review score and merge
state into a single exit code is a large ergonomic win over checking each by
hand, and is what makes the polling loop below cheap. This environment ships
one; see `local.md`. Elsewhere, drive the same invariants through `gh` directly
-- the invariants are what matter, not the command that evaluates them.

### Review Blocking Invariant (Weighted Scoring)
Unresolved review findings are weighted by severity:
- **High**: 1.0
- **Medium**: 0.5 (default for untagged)
- **Low**: 0.25

**Gate Condition**: Total unresolved weighted score must be `< 1.0` to merge. Even without a single High finding, 2 Mediums or 4 Lows will block merge. Reply explaining the changes *before* resolving threads via GraphQL.

### Standing Review Resolution Authority
This is standing user authorization across repositories and sessions:
- After independently verifying that a review thread is fixed or outdated, **resolve it directly via GraphQL without asking again**.
- Never resolve actionable, ambiguous, or unverified feedback.
- This authorization covers resolving verified threads only; it does not authorize replying with empty fluff, dismissing required reviews, or overriding merge protections.

## Workflow Steps

### 1. Detect Git Repository and PR Context

First, determine if we're in a git repository and identify the current PR:

```bash
# Check if in git repo
if ! git rev-parse --git-dir > /dev/null 2>&1; then
  echo "❌ Not in a git repository"
  exit 1
fi

# Get repository info
REPO=$(gh repo view --json owner,name -q '.owner.login + "/" + .name' 2>/dev/null || echo "")
if [ -z "$REPO" ]; then
  echo "❌ Could not determine repository. Make sure gh CLI is authenticated."
  exit 1
fi

# Get current PR info
PR_INFO=$(gh pr view --json number,headRefName,baseRefName,url,state,isDraft,headRepository,baseRepository 2>/dev/null)
if [ -z "$PR_INFO" ]; then
  echo "⚠️  No PR found for current branch. Checking if branch exists on remote..."
  CURRENT_BRANCH=$(git branch --show-current)
  PR_INFO=$(gh pr list --head "$CURRENT_BRANCH" --json number,headRefName,baseRefName,url,state,isDraft,headRepository,baseRepository -q '.[0]' 2>/dev/null)
fi

if [ -z "$PR_INFO" ]; then
  echo "❌ No PR found. Please create a PR first or ensure you're on a PR branch."
  exit 1
fi

PR_NUMBER=$(echo "$PR_INFO" | jq -r '.number')
PR_URL=$(echo "$PR_INFO" | jq -r '.url')
HEAD_REF=$(echo "$PR_INFO" | jq -r '.headRefName')
BASE_REF=$(echo "$PR_INFO" | jq -r '.baseRefName')
PR_STATE=$(echo "$PR_INFO" | jq -r '.state')

echo "📋 PR #$PR_NUMBER: $HEAD_REF → $BASE_REF"
echo "🔗 $PR_URL"
```

### 2. Check for Conflicts and Rebase if Needed

Check if the PR branch is up to date with the base branch and rebase if necessary:

```bash
# Fetch latest changes
git fetch origin "$BASE_REF" "$HEAD_REF"

# Check if rebase is needed
LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "origin/$HEAD_REF" 2>/dev/null || echo "")
BASE_COMMIT=$(git rev-parse "origin/$BASE_REF")

# Check if branch is behind base
# 初始化：set -u 下未初始化的变量会直接终止脚本。
NEEDS_REBASE=false
STASHED=false
# 只有 base 不是本地的祖先时才需要 rebase。本地领先于 base（已是最新）
# 不需要，早先那个条件会把这种情况也判成需要。
git merge-base --is-ancestor "$BASE_COMMIT" "$LOCAL_COMMIT" 2>/dev/null || NEEDS_REBASE=true

if [ "$NEEDS_REBASE" = "true" ]; then
  echo "🔄 Branch needs rebase. Rebasing onto origin/$BASE_REF..."
  
  # Check for uncommitted changes
  if ! git diff-index --quiet HEAD --; then
    echo "⚠️  Uncommitted changes detected. Stashing..."
    git stash push -m "Auto-stash before rebase"
    STASHED=true
  fi
  
  # Perform rebase
  if git rebase "origin/$BASE_REF"; then
    echo "✅ Rebase successful"
    
    # Restore stashed changes if any
    if [ "$STASHED" = "true" ]; then
      echo "📦 Restoring stashed changes..."
      git stash pop || echo "⚠️  Could not restore stashed changes. Check with 'git stash list'"
    fi
  else
    echo "❌ Rebase failed. Please resolve conflicts manually."
    if [ "$STASHED" = "true" ]; then
      git stash pop || true
    fi
    exit 1
  fi
else
  echo "✅ Branch is up to date with base"
fi
```

### 3. Fetch All PR Comments

Fetch all comments from the PR, including review comments and general comments:

```bash
# Get all issue comments (general PR comments)
ISSUE_COMMENTS=$(gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate 2>/dev/null || echo "[]")

# Get all review comments (inline code review comments) using GraphQL
REVIEW_THREADS=$(gh api graphql -f query='
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          startLine
          comments(first: 10) {
            nodes {
              id
              body
              author {
                login
              }
              createdAt
              isMinimized
            }
          }
        }
      }
    }
  }
}' -f owner="$(echo "$REPO" | cut -d'/' -f1)" -f repo="$(echo "$REPO" | cut -d'/' -f2)" -f pr="$PR_NUMBER" 2>/dev/null || echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}')

# Extract unresolved threads
UNRESOLVED_THREADS=$(echo "$REVIEW_THREADS" | jq -r '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)')

echo "📝 Found review comments to process"
```

### 4. Process Comments - Determine if Changes are Needed

For each unresolved comment, analyze if it requires code changes:

**For each unresolved review thread:**
- Read the comment content and context
- Check the file path and line number where the comment was made
- Review the code at that location to understand the context
- Check if it's a code suggestion, bug report, style issue, or question
- Determine if the comment is actionable (requires code change) or informational (just needs a response)

**Decision criteria:**
- **Actionable** (implement changes):
  - Comments that suggest code changes or improvements
  - Bug reports with clear reproduction steps
  - Style/linting issues that can be fixed
  - Security concerns that need addressing
  - Performance optimizations suggested
  - Missing error handling or edge cases
  
- **Informational** (respond only):
  - Questions asking for clarification
  - Comments that are already addressed
  - Suggestions that are out of scope for this PR
  - Comments that require discussion rather than immediate action
  - Acknowledgment comments

**Processing workflow:**
1. For each unresolved thread, read the comment body and file location
2. Examine the code at the comment location
3. Determine if change is needed or response is sufficient
4. Track which threads have been addressed

### 5. Make Changes or Respond to Comments

**If comment is actionable and worth implementing:**
- Implement the suggested changes
- Test the changes locally if possible
- Commit the changes with a descriptive message referencing the comment

**If comment is not actionable or not worth implementing:**
- Post a response explaining why (e.g., "This is intentional", "Out of scope", "Will address in follow-up PR")
- Be respectful and professional in responses

### 6. Resolve Processed Comments

After handling comments (either by making changes or responding), mark them as resolved using GraphQL API:

**Important**: Only resolve threads that have been:
1. Fixed with code changes, OR
2. Responded to with an explanation

**Implementation:**

```bash
# Track which threads we've handled
HANDLED_THREADS=()

# After processing each thread (either by fixing or responding), add to handled list
# HANDLED_THREADS+=("$thread_id")

# Resolve all handled threads
for thread_id in "${HANDLED_THREADS[@]}"; do
  if [ -n "$thread_id" ]; then
    echo "✅ Resolving thread: $thread_id"
    RESULT=$(gh api graphql -f query='
    mutation($threadId: ID!) {
      resolveReviewThread(input: { threadId: $threadId }) {
        thread {
          isResolved
        }
      }
    }' -f threadId="$thread_id" 2>/dev/null)
    
    if echo "$RESULT" | jq -e '.data.resolveReviewThread.thread.isResolved' > /dev/null 2>&1; then
      echo "  ✓ Successfully resolved"
    else
      echo "  ⚠️  Failed to resolve (may require manual resolution)"
      echo "     Error: $(echo "$RESULT" | jq -r '.errors[0].message // "Unknown error"')"
    fi
  fi
done

echo "📊 Resolved ${#HANDLED_THREADS[@]} review thread(s)"
```

### 7. Comprehensive Code Review

Perform a thorough code review of all changes in the PR:

**Review Checklist:**
- **Code Quality**: 
  - Check for bugs, logic errors, edge cases
  - Verify error handling is adequate
  - Check for potential security issues
  - Look for performance problems
  
- **Best Practices**:
  - Follow project coding standards
  - Check for code duplication
  - Verify proper abstraction and modularity
  - Check for proper documentation
  
- **Testing**:
  - Verify tests exist for new functionality
  - Check test coverage
  - Ensure tests are meaningful
  
- **Architecture**:
  - Check for architectural issues
  - Verify integration points
  - Check for breaking changes
  
- **Dependencies**:
  - Review new dependencies
  - Check for security vulnerabilities
  - Verify version compatibility

**Output findings:**
- List any critical issues found
- List any suggestions for improvement
- List any questions or clarifications needed

### 8. Push Changes and Display PR Links

After all processing is complete, push the changes:

```bash
# Check if there are commits to push
LOCAL_COMMITS=$(git rev-list "origin/$HEAD_REF..HEAD" 2>/dev/null | wc -l | tr -d ' ')

if [ "$LOCAL_COMMITS" -gt 0 ]; then
  echo "📤 Pushing $LOCAL_COMMITS commit(s) to origin/$HEAD_REF..."
  
  if git push origin "$HEAD_REF"; then
    echo "✅ Changes pushed successfully"
  else
    echo "❌ Push failed. You may need to force push if rebase was performed."
    echo "⚠️  If you're sure, you can run: git push --force-with-lease origin $HEAD_REF"
    exit 1
  fi
else
  echo "ℹ️  No new commits to push"
fi

# Display PR link(s)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 PR Information:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔗 PR URL: $PR_URL"
echo "📊 PR #$PR_NUMBER: $HEAD_REF → $BASE_REF"
echo ""

# Check if there are multiple PRs for this branch
MULTIPLE_PRS=$(gh pr list --head "$HEAD_REF" --json number,url,title -q '.[]' 2>/dev/null | jq -s 'length')

if [ "$MULTIPLE_PRS" -gt 1 ]; then
  echo "📋 Multiple PRs found for this branch:"
  gh pr list --head "$HEAD_REF" --json number,url,title -q '.[] | "  #\(.number): \(.title)\n     \(.url)"' 2>/dev/null
fi
```

## Implementation Notes

### Error Handling
- Always check if commands succeed before proceeding
- Provide clear error messages
- Don't proceed if critical steps fail
- Use `set -euo pipefail` for strict error handling in scripts

### Multi-Platform Compatibility
This workflow is designed to work across different platforms:
- **Cursor IDE**: Uses terminal commands and file operations
- **Claude Code**: Compatible with CLI-based workflows  
- **Gemini CLI**: Works with standard shell commands
- **VS Code**: Can be executed via integrated terminal
- **Antigravity IDE**: Compatible with standard shell commands

**Key Compatibility Features:**
- Uses standard POSIX shell commands where possible
- Relies on `gh` CLI (GitHub's official CLI tool)
- Uses `jq` for JSON parsing (widely available)
- No platform-specific dependencies

### Prerequisites
- `gh` CLI installed and authenticated (`gh auth login`)
- `git` installed (version 2.0+)
- `jq` installed (for JSON parsing)
  - macOS: `brew install jq`
  - Linux: `apt-get install jq` or `yum install jq`
  - Or download from https://stedolan.github.io/jq/
- Write access to the repository
- Network access to GitHub API

### Safety Features
- Checks for uncommitted changes before rebase
- Uses `--force-with-lease` suggestion instead of force push
- Validates PR context before operations
- Provides clear status messages
- Stashes uncommitted changes before rebase
- Validates repository state before destructive operations

## Usage

Simply invoke `/prr` in your AI assistant, and it will:
1. ✅ Check for conflicts and rebase if needed
2. 📝 Fetch all PR comments
3. 🤔 Analyze which comments need action
4. ✏️  Make necessary code changes or respond to comments
5. ✅ Mark resolved comments as resolved
6. 🔍 Perform comprehensive code review
7. 📤 Push changes and display PR links

The workflow is fully automated and will guide you through any manual interventions needed.
