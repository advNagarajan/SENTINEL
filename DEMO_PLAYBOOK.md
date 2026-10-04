# SENTINEL Demo Playbook: Autonomous Mainframe Agent & Knowledge Graph

This playbook outlines an end-to-end live demonstration of **SENTINEL** controlling an IBM Mainframe (TK4-/TK5 / z/OS) via the **Google Gemini CLI**, while concurrently building a real-time **Neo4j State-Transition Knowledge Graph**.

---

## 1. System Architecture

```text
  ┌─────────────────────────────────────────────────────────┐
  │                 Gemini Agent (Gemini CLI)               │
  └────────────────────────────┬────────────────────────────┘
                               │  stdio (MCP)
  ┌────────────────────────────▼────────────────────────────┐
  │                   SENTINEL MCP Server                   │
  │                   (layer4.mcp_server)                   │
  └────────────────────────────┬────────────────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
 ┌───────────────────────┐            ┌───────────────────────┐
 │  RuntimeOrchestrator  │            │   Navigator Emitter   │
 │   & Layer 3 Gateway   │            │     (HTTP :8100)      │
 └──────────┬────────────┘            └───────────┬───────────┘
            │                                     │
            ▼                                     ▼
 ┌───────────────────────┐            ┌───────────────────────┐
 │   TN3270 Driver (L1)  │            │     Neo4j Database    │
 │       Port 3270       │            │      (:7474 UI)       │
 └──────────┬────────────┘            └───────────────────────┘
            │
            ▼
 ┌───────────────────────┐
 │ Hercules MVS / ISPF   │
 └───────────────────────┘
```

---

## 2. Pre-Flight Checklist

Before starting your presentation, ensure these 3 services are active:

| Service | Port / URL | Status Check Command |
| :--- | :--- | :--- |
| **Hercules / TK5** | `localhost:3270` | `docker ps` (container `tk5-hercules` active) |
| **Neo4j Graph Database** | `http://localhost:7474` | Browser opens login (`neo4j` / `sentinel_graph`) |
| **Navigator Service** | `http://localhost:8100/health` | Returns `{"status":"healthy","neo4j":"connected"}` |

### Starting Neo4j & Navigator:
```powershell
docker compose up -d
```

### Launching Gemini CLI:
Open a PowerShell window in the project root:
```powershell
cd c:\Users\aadha\Desktop\SENTINEL
$env:GEMINI_API_KEY = "your-gemini-api-key"
gemini
```

---

## 3. The 3-Act Live Demo Script

### Act 0: Initial Observation (The Handshake)
Verify the automatic session bootstrapper dropped the agent directly into ISPF.

**Prompt to Gemini:**
> *"Inspect the mainframe screen using `get_observation`. Tell me what screen we are on, what the title is, what fields are editable, and report the active generation token."*

**Expected Output:**
- Screen Title: `ISPF PRIMARY OPTION MENU`
- Generation: `#1`
- Editable Fields: `fld_option_===>`
- Status: `OIA: READY`

---

### Act 1: The Fastpath & Record Browser (Visual & Instant)
Demonstrates semantic field typing, reading system datasets, and native 3270 pagination.

**Prompt to Gemini:**
> *"From the ISPF Primary Option Menu:
> 1. Select option '1' (Browse) with ENTER.
> 2. In the Browse entry panel, enter data set name `'SYS1.PROCLIB'` and submit with ENTER.
> 3. Report the first 5 records you see on screen."*

**Follow-up Prompt (Pagination):**
> *"Scroll down through the records using action 'PF8', then press 'PF3' twice to return to the ISPF Primary Option Menu."*

**Talking Point for Audience:**
> *"The agent doesn't need hardcoded row/column coordinates or visual OCR. SENTINEL compiles the 80x24 buffer into semantic fields, and the agent navigates using native 3270 AID keys like PF8 (Page Down) and PF3 (Exit/Back)."*

---

### Act 2: Mainframe Record & Dataset Allocation (The "Showstopper")
Demonstrates `fill_form_and_submit` populating technical mainframe record parameters: **Fixed-Block (FB)**, **Record Length 80 (LRECL)**, **Block Size 3120 (BLKSIZE)**, and **Tracks**.

**Prompt to Gemini:**
> *"Navigate to ISPF Data Set Utility by entering option '3.2' and pressing ENTER. 
> On the Data Set Utility screen, enter option 'A' (Allocate) with data set name `HERC01.DEMO.PDS` and submit. 
> Tell me what allocation parameter fields are requested."*

**Follow-up Prompt (Multi-field Form Fill):**
> *"Using `fill_form_and_submit`, populate the allocation attributes with:
> - Space units: `TRACKS`
> - Primary quantity: `10`
> - Secondary quantity: `5`
> - Directory blocks: `10`
> - Record format: `FB`
> - Record length: `80`
> - Block size: `3120`
> Submit with ENTER and verify the top-right status message confirms 'Data set allocated'."*

**Talking Point for Audience:**
> *"Notice the generation token guard: if the mainframe delays or drops into an X-SYSTEM inhibit state, SENTINEL locks downward actions until the OIA settles before generating the next state token."*

---

### Act 3: Verification in DSLIST (ISPF 3.4) & Clean Teardown
Demonstrates fastpath jumping (`=3.4`), catalog verification, and safe exit.

**Prompt to Gemini:**
> *"Jump directly to the Data Set List Utility using fastpath `=3.4`. 
> Enter `HERC01.*` in the Dsname Level field and submit with ENTER. 
> Confirm that our new dataset `HERC01.DEMO.PDS` appears in the catalog list."*

**Return to Main Menu:**
> *"Send 'PF3' to back out cleanly to the ISPF Primary Option Menu."*

---

## 4. Live Neo4j Knowledge Graph Showcase

During or immediately following the demo, switch your screen to your browser at **`http://localhost:7474`**.

- **Connect URL**: `bolt://localhost:7687`
- **Username**: `neo4j`
- **Password**: `sentinel_graph`

### Live Cypher Queries to Run for the Audience:

#### 1. View the entire state-transition graph created by the agent:
```cypher
MATCH (s:Screen)-[r:TRANSITION]->(t:Screen)
RETURN s, r, t
LIMIT 50
```

#### 2. Inspect transition latencies and action types:
```cypher
MATCH ()-[r:TRANSITION]->()
RETURN r.action_signature AS Action, 
       r.tool_name AS Tool, 
       r.latency_ms AS LatencyMs, 
       r.timestamp AS Time
ORDER BY r.timestamp DESC
LIMIT 10
```

#### 3. Find all paths leading to ISPF:
```cypher
MATCH path = (start:Screen)-[:TRANSITION*1..5]->(end:Screen)
WHERE end.title CONTAINS "ISPF"
RETURN path
LIMIT 10
```

---

## 5. Key Architecture Differentiators (Q&A Cheatsheet)

1. **Why not use computer vision (VLM)?**
   - Mainframes operate via deterministic 3270 data streams (EBCDIC/SBA orders, attribute bytes, OIA status). SENTINEL reads the actual protocol stream with 100% character fidelity, avoiding hallucinated characters or bounding-box jitter.
2. **How does SENTINEL prevent race conditions?**
   - The OIA Stability Engine monitors inhibit bits (`X SYSTEM`, `X [Wait]`) and guarantees screen quiescence before emitting state.
   - Optimistic concurrency tokens (`generation_token`) ensure the agent can never execute an action against a stale or moving screen.
3. **What is the Neo4j Navigator used for?**
   - It autonomously maps the legacy application's hidden state machine as the agent explores, enabling offline shortest-path calculation, transition prediction, and legacy system discovery.
