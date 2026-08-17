"""_judge_file_context: git diff priority, blob fallback, TRUNCATED markers.

Changes in verification_service._judge_file_context:
- tries git diff HEAD~1 HEAD first (shows only the changed lines)
- falls back to file blobs when diff is empty or fails
- appends "[... TRUNCATED ...]" sentinel when content exceeds the byte cap
- returns sentinel string when all reads fail so auto-promotion fires
"""
