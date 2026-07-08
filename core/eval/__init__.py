"""Automated response-quality evaluation (CLAUDE.md §6/§7).

The LLM-as-judge that scores a companion reply against the design's response
standard, and the per-turn evaluator that posts those scores to the eval backend
(Langfuse) so quality is inspectable next to the pipeline that produced it.
"""
