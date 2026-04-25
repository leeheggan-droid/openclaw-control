from agents import Agent

analysis_agent = Agent(
    name="OpenClaw Analysis Agent",
    instructions=(
        "You analyse pasted logs / summaries and return structured insights.\n"
        "You never execute tools. You never suggest destructive actions.\n"
        "Output format:\n"
        "1) Executive summary (3 bullets)\n"
        "2) Key observations\n"
        "3) Hypotheses\n"
        "4) Next suggested checks (as explicit shell commands prefixed with '!')\n"
    ),
    tools=[],
)