# AGENTS

Last reviewed: 2026-03-05

## Quick Map
- Knowledge base index: docs/index.md
- Core beliefs: docs/core-beliefs.md
- Architecture: ARCHITECTURE.md
- Plans (active): docs/plans/active/
- Plans (completed): docs/plans/completed/
- Plan template: docs/plans/plan-template.md
- Tech debt: docs/plans/tech-debt-tracker.md

## Commands
- Knowledge check: python3 scripts/knowledge_check.py

## Answer Then Act
- Answer the user's question directly.
- If the truthful answer to a sufficiency, completeness, or quality question is "no" and the concrete fix is inferable from the repo and thread context, implement the fix instead of stopping at analysis.
- If the truthful answer is "yes", report that and stop.
- If a real blocker remains, report the blocker clearly.

## Notes
- Keep this file short; link out for details.
