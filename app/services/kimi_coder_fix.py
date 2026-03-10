# Fix for kimi_coder.py line ~446
# Change:
#     if isinstance(result, str) and "[ERROR]" in result:
# To:
#     if isinstance(result, str) and result.startswith("[ERROR]"):
# 
# This prevents false positives when self_read_file reads files containing "[ERROR]" as example text.
