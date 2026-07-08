# Fast-LLM Latency + Quality Benchmark

_Generated 2026-07-09 01:57 — 3 runs × 3 representative companion turns per model (reply tier, thinking off, temp 0.7, max_tokens 300). Latency is the per-call round-trip through OpenRouter; quality is the companion-voice LLM-judge (1–5, higher better)._

| Model | Median | Min | Max | Quality (avg /5) | Chatbot flags |
|---|---|---|---|---|---|
| `google/gemini-2.5-flash` | **966ms** | 747ms | 2246ms | 3 | 1 |
| `openai/gpt-4.1-mini` | **1091ms** | 923ms | 3590ms | 4.3 | 0 |
| `openai/gpt-4.1-nano` | **1240ms** | 931ms | 1632ms | 4 | 0 |
| `anthropic/claude-haiku-4.5` | **1637ms** | 1185ms | 2355ms | 5 | 0 |
| `google/gemini-2.5-flash-lite` | **2148ms** | 798ms | 2853ms | 4.3 | 0 |

## Notes
- Ranked by median latency (fastest first).
- Quality is scored by the pinned companion-voice judge on each reply; a good model is fast AND ≥4/5 with 0 chatbot flags.
- The app's default fast/reply tier is set in `config/defaults/provider_config.json` (`llm_router.tiers`). Pick the fastest model that holds quality.

### Sample replies
- `google/gemini-2.5-flash` → 'That depends, what does "push through" mean for you this weekend?'
- `openai/gpt-4.1-mini` → 'Take the break if you’re feeling worn out—rest usually fuels better work later.'
- `openai/gpt-4.1-nano` → "If you're feeling tired, a break might do you good—sometimes pushing through just makes it worse."
- `anthropic/claude-haiku-4.5` → "Depends what you're running on right now — if you're already coasting on fumes, pushing through usually just means you'l"
- `google/gemini-2.5-flash-lite` → "That really depends on how you're feeling – are you burnt out or just a little tired?"
