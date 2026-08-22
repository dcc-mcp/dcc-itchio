import os
import sys

import dcc_mcp_core

SKILL_ROOTS = (
    os.path.join("skill", "itchio-assets"),
    os.path.join("skill", "itchio-publish"),
)


def main():
    failed = False
    for relative_root in SKILL_ROOTS:
        skill_root = os.path.abspath(relative_root)
        report = dcc_mcp_core.validate_skill(skill_root)
        if report.issues:
            for issue in report.issues:
                print("{} {}: {}".format(issue.severity, issue.category, issue.message))
        else:
            print("clean: {}".format(relative_root))
        failed = failed or report.has_errors or not report.is_clean
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
