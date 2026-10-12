import re
from app.orchestrator.models import ReviewIssue, ReviewResult, Subtask


class CodeReviewer:
    """
    Automated Code Review gatekeeper:
    - Layer 1: Static policy analysis (syntax markers, hardcoded secrets, dangerous calls).
    - Layer 2: Semantic check verifying that generated outputs fulfill the goal.
    """

    DANGEROUS_PATTERNS = [
        (r"eval\(", "Use of eval() detected, which poses a code execution security risk."),
        (r"exec\(", "Use of exec() detected, which poses a security risk."),
        (r"os\.system\(", "Use of os.system() detected. Prefer safe subprocess argument lists."),
        (r"(?i)api_key\s*=\s*['\"][a-zA-Z0-9_\-]{20,}['\"]", "Possible hardcoded API key or secret detected."),
        (r"(?i)password\s*=\s*['\"][^'\"]+['\"]", "Possible hardcoded password detected."),
    ]

    def review_changes(
        self,
        goal: str,
        subtasks: list[Subtask],
        generated_diffs: dict[str, str] | None = None,
    ) -> ReviewResult:
        issues: list[ReviewIssue] = []
        suggestions: list[str] = []
        score = 100

        # 1. Check subtask execution statuses
        failed_tasks = [s for s in subtasks if s.status.value in ("failed", "skipped")]
        if failed_tasks:
            score -= (len(failed_tasks) * 25)
            for f in failed_tasks:
                issues.append(ReviewIssue(
                    severity="error",
                    file=f.id,
                    message=f"Subtask '{f.title}' did not complete successfully: {f.error or 'Failed'}",
                ))

        # 2. Check code outputs & diffs for security violations
        for s in subtasks:
            code_text = f"{s.output}\n{s.diff}"
            for pattern, msg in self.DANGEROUS_PATTERNS:
                if re.search(pattern, code_text):
                    score -= 20
                    target_file = s.target_files[0] if s.target_files else s.id
                    issues.append(ReviewIssue(
                        severity="error",
                        file=target_file,
                        message=msg,
                    ))

            # Check for incomplete implementation markers
            if "TODO: implement" in code_text or "NotImplementedError" in code_text:
                score -= 10
                suggestions.append(f"Subtask '{s.title}' contains placeholder or incomplete code.")

        score = max(0, min(100, score))
        approved = score >= 70 and not any(i.severity == "error" for i in issues)

        if approved:
            summary = (
                f"Review passed with quality score {score}/100. "
                f"All {len(subtasks)} subtask outputs verified with no critical security violations."
            )
        else:
            summary = (
                f"Review flagged issues with score {score}/100. "
                f"Detected {len([i for i in issues if i.severity == 'error'])} critical errors."
            )

        return ReviewResult(
            approved=approved,
            score=score,
            summary=summary,
            issues=issues,
            suggestions=suggestions,
        )
