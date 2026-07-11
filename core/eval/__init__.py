"""Automated response-quality evaluation (design doc: response-quality evaluation).

The LLM-as-judge that scores a companion reply against the design's response
standard, and the per-turn evaluator that posts those scores to the eval backend
(Langfuse) so quality is inspectable next to the pipeline that produced it.
"""
