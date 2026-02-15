"""CLI chatbot for testing the multi-agent system locally."""

import asyncio
import uuid

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from graph import get_checkpointer, build_graph


async def main():
    checkpointer = get_checkpointer()
    agent = build_graph(checkpointer)

    thread_id = str(uuid.uuid4())
    user_id = input("Enter user_id (e.g. fridaklo): ").strip() or "fridaklo"

    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
        }
    }

    print(f"\nMulti-Agent CLI — user: {user_id}, thread: {thread_id[:8]}...")
    print("Type 'quit' to exit, 'resume' to simulate bank login resume\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() == "quit":
            break

        if user_input.lower() == "resume":
            resume_data = {"login_status": "success"}
            print(f"  [Resuming with: {resume_data}]")
            result = await agent.ainvoke(Command(resume=resume_data), config)
        else:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=user_input)]},
                config,
            )

        # Check for interrupts
        state = await agent.aget_state(config)
        has_interrupt = False
        if state.next:
            for task in state.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    interrupt_data = task.interrupts[0].value
                    print(f"\n  [INTERRUPT: {interrupt_data}]")
                    print("  Type 'resume' to simulate bank login completion\n")
                    has_interrupt = True
                    break

        # Print last AI message (skip when interrupted — last AI msg is stale)
        if not has_interrupt:
            messages = state.values.get("messages", [])
            for msg in reversed(messages):
                if hasattr(msg, "type") and msg.type == "ai" and msg.content:
                    print(f"\nAgent: {msg.content}\n")
                    break


if __name__ == "__main__":
    asyncio.run(main())
