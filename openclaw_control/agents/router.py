from dataclasses import dataclass

@dataclass(frozen=True)
class Route:
    name: str

def route_message(text: str) -> Route:
    t = (text or "").strip().lower()

    # direct mode remains handled by the UI (prefix '!')
    if not t:
        return Route("ops")

    # Heuristics: if user pasted a lot of text, treat as analysis
    if len(t) > 1500 or "replay summary" in t or "blocking reasons" in t:
        return Route("analysis")

    # Default: ops helper
    return Route("ops")