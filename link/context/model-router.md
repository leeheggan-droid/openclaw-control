# Model Router — Cost Optimisation

> **Status:** Active (deployed 2026-05-06)  
> Link is aware that its chat API automatically routes requests to cheaper models.
> Claude is still used whenever tools are invoked or the task is complex.

---

## Overview

The Link chat route (`src/app/api/chat/route.ts`) uses an intelligent model router
(`src/lib/modelRouter.ts`) to select the cheapest capable model for each turn.
No manual action is required — routing is automatic.

---

## Routing Rules

| Task type | Model | Cost |
|---|---|---|
| Simple reads / context lookups | Groq | Free |
| Code generation | Gemini Flash | ~$0.075/1M tokens |
| Complex decisions / analysis | Claude Sonnet | ~$3/1M tokens |
| Any turn that uses tools (GitHub, Kraken, Alpaca…) | Claude Sonnet (forced) | ~$3/1M tokens |

**Key rule:** Tools always force Claude. If `activeTools.length > 0`, the router
sends the request to Claude regardless of task classification.

---

## Estimated Cost Impact

| Scenario | Monthly cost |
|---|---|
| Before (all Claude, ~100 interactions/day) | ~$27/month |
| After (intelligent routing, same volume) | ~$2.85/month |
| Saving | ~$24/month (89%) |

---

## Environment Variables Required

The router reads these from the runtime environment (already set on Vercel):

| Variable | Provider | Used for |
|---|---|---|
| `GROQ_API_KEY` | Groq | Free-tier simple reads |
| `GEMINI_API_KEY` or `GEMINI_API_KEY_2` | Google | Code generation |
| `ANTHROPIC_API_KEY` | Anthropic | Complex tasks + all tool use |

---

## Identifying Which Model Was Used

Check Vercel function logs (or runtime logs wherever Link is deployed) for:

```
[model router] Used groq for this turn
[model router] Used gemini for this turn
[model router] Used claude for this turn
```

---

## Verification Tests

Run these as user messages after deployment to confirm routing:

| Test | Expected log entry |
|---|---|
| "What's in context/alpaca-bot-system.md?" | `Used groq` |
| "Write a function to format dates" | `Used gemini` |
| "Should we deploy the cost optimisation now?" | `Used claude` |
| "Check my Kraken balance" | `Used claude` (tool use forces Claude) |

---

## Fallback Behaviour

If Groq or Gemini fail (API error, rate limit, etc.) the router automatically
falls back to Claude. The API interface to callers is unchanged — no errors are
surfaced due to a model switch.

---

## Rollback

If routing causes unexpected behaviour, revert in the Link repo:

```bash
git revert HEAD   # undo the router integration commit
# Push to main → Vercel auto-deploys the reverted build
```

No VPS action is needed — Link runs on Vercel.

---

## Files Changed (Link Repo)

| File | Change |
|---|---|
| `src/lib/modelRouter.ts` | New file — routing logic |
| `src/app/api/chat/route.ts` | Replaced `anthropic.messages.stream` block with `routeToModel` call |
| `package.json` | Added `@google/generative-ai` dependency |
