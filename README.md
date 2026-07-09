
<div align="center">

<pre>
   _____                  ____     ____        _ _     _           
  / ____|           _    / / /    |  _ \      (_) |   | |          
 | |  __  ___  _ __(_)  / / /_ _  | |_) |_   _ _| | __| | ___ _ __ 
 | | |_ |/ _ \| '__|   / / / _` | |  _ <| | | | | |/ _` |/ _ \ '__|
 | |__| | (_) | |   _ / / / (_| | | |_) | |_| | | | (_| |  __/ |   
  \_____|\___/|_|  (_)_/_/ \__,_| |____/ \__,_|_|_|\__,_|\___|_|   
  
</pre>

**An AI coding agent that builds, previews, and deploys full-stack apps from a chat prompt. Built for Indies.**

[![SPONSORED BY E2B FOR STARTUPS](https://img.shields.io/badge/SPONSORED%20BY-E2B%20FOR%20STARTUPS-ff8800?style=for-the-badge)](https://e2b.dev/startups)
[![Discord](https://img.shields.io/badge/Discord-Join_the_Beta-7289da?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/V3f3PkwQbY)
[![Website](https://img.shields.io/badge/Website-gorillabuilder.dev-00ff41?style=for-the-badge)](https://gorillabuilder.dev)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-GorillaBuilder-blue?style=for-the-badge&logo=linkedin)](https://www.linkedin.com/in/gorillabuilder)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

</div>

---

## What it is

**Gorilla Builder** takes a prompt (or a Figma/Miro import) and drives an agent loop — plan, write/edit files, run commands, verify the dev server, repeat — inside an isolated [E2B](https://e2b.dev) sandbox, streaming every step back to the browser in real time. The result is a working React/Node app you can preview live, edit by hand, and push to GitHub or Vercel.

It's a FastAPI backend (`app.py`, `backend/`) driving a Python agent (`backend/ai/lineage_agent.py`) that operates on sandboxed containers (`backend/e2b_sandbox.py`), paired with a server-rendered frontend (`frontend/`) using SSE for live progress.

## How the agent loop works

* **Plan → code → verify.** The agent (`lineage_agent.py`) expands the prompt into a plan, emits tool calls (`write_file`, `edit_file`, `read_files`, `grep_search`, `run_bash`, ...), and the sandbox manager (`e2b_sandbox.py`) executes them against a live container.
* **Sandboxed execution.** Every project runs in its own E2B sandbox — file writes, shell commands, and the dev server are fully isolated per project, with paths strictly confined to the project root.
* **Live tool-call visibility.** The frontend renders each tool call as an expandable activity card (running → done/failed) as it happens, not just a final "done" message — plus syntax-highlighted, copyable code blocks in the chat itself.
* **Self-healing dev server.** Before the agent can mark a task done, it verifies the dev server is actually responding; restart loops are blocked in favor of fixing the underlying error.
* **Structured providers.** Optional Supabase auth/database provisioning and Figma/Miro import are handled as first-class tool integrations, not bolted-on scripts.

## Status

This is an active work-in-progress, not a polished 1.0. Expect rough edges. Recent hardening work has focused on:

* Confining all agent file I/O (read/write/list/grep/delete) to the project sandbox root, closing several path-traversal edge cases.
* Making sandbox/dev-server failures surface to the user instead of failing silently.
* Preventing duplicate concurrent agent runs against the same project/sandbox.
* Modernizing the chat UI (markdown rendering, syntax-highlighted code blocks with copy buttons, tool-call activity cards) and removing dead/duplicated frontend dependencies.

## Running it locally

The app expects ~25 environment variables (Supabase, E2B, OAuth providers, OpenRouter/model keys, etc. — see `backend/settings.py`). There's no local "just works" mode yet; you need real credentials for Supabase and E2B at minimum to boot a project.

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

## 🤝 Join the Swarm (Private Beta)

We are actively recruiting systems engineers, AI architects, and hackers to push the limits of this engine. We need help optimizing AST parsers, improving the WebContainer memory footprint, and expanding the AgentSkills via MCP.

**Private Beta & 2.5M Tokens:**
The hosted platform operates on an ad-subsidized free tier (500k tokens) and a $13.99 Pro tier (5 million tokens). 

If you contribute to the core engine or join our beta testing cohort, we will upgrade your hosted Gorilla Builder account to a **2.5 MILLION token Premium Beta role**. 

Join the [Discord](https://discord.gg/V3f3PkwQbY) to claim your role, report bugs, and give feedback.

## 🔗 Links

- **Website:** [gorillabuilder.dev](https://gorillabuilder.dev)
- **Discord:** [Join the Beta](https://discord.gg/V3f3PkwQbY)
- **LinkedIn:** [GorillaBuilder](https://www.linkedin.com/in/gorillabuilder)
- **GitHub:** [Gorilla-builder](https://github.com/GorillaBuilder/Gorilla-builder)

## 🛡️ License

Distributed under the MIT License. See `LICENSE` for more information.
