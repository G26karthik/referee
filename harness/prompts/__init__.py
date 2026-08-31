"""Prompt text for each subagent, kept out of the stage code files.

One module per stage: exports `SYS` (the system/persona prompt) and a `build(...)`
function that returns the user prompt. Stage modules import these and stay lean.
"""
