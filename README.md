# GridWise Energy Optimizer ⚡

A robust, LLM-assisted HTTP API for 24-hour smart campus energy scheduling, built for the **BUP CSE Fest 2026 Hackathon**.

---

## 🏗 Architecture & Stack

This system operates as a strict sequential pipeline, ensuring invalid AI generations never crash the deterministic math solver.

- **LLM Provider:** Groq (`gpt-oss-20b`) operating in strict JSON mode with temperature 0.
- **Math Optimizer:** HiGHS linear programming solver (`highspy` via PuLP).
- **Data Structures:** Enforced use of lists over maps for internal memory aggregates and processing.

```text
[HTTP POST] -> [Validation Guardrails] -> [Groq LLM] -> [Guardrail Coercion] -> [HiGHS Optimizer] -> [JSON Response]
```

---

## 🚀 Quickstart Guide

### Option A: Native Python (3 Steps)

1. **Clone & Install:**
   ```bash
   git clone <your-repo-url>
   cd <repo-folder>
   pip install -r requirements.txt
   ```

2. **Configure Environment:**  
   Create a `.env` file in the root directory:
   ```env
   GROQ_API_KEY=gsk_your_groq_api_key_here
   GROQ_MODEL=openai/gpt-oss-20b
   SOLVER_TIME_LIMIT_SECONDS=10
   PORT=8000
   ```

3. **Run the Server:**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

---

### Option B: Docker Fallback (3 Steps)

1. **Pull the Image:**
   ```bash
   docker pull <dockerhub-user>/gridwise-optimizer:latest
   ```

2. **Run the Container:**
   ```bash
   docker run -d -p 8000:8000 -e GROQ_API_KEY="gsk_your_groq_api_key_here" <dockerhub-user>/gridwise-optimizer:latest
   ```

3. **Verify Health:**
   ```bash
   curl http://localhost:8000/health
   ```

---

## 🧪 Testing the API

Execute a sample request (using the provided `SAMPLE-06`) to verify the expected optimal cost of **34,090 BDT**:

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @examples/sample_request.json
```

---

## 🛡️ Failure Behavior & Guardrails

| LLM Failure | Guardrail Action | Optimizer Status |
| :--- | :--- | :--- |
| **Provider Timeout / 429** | Uses regex / rule-based fallback parser | Proceeds with fallback parsed data |
| **Invalid JSON** | Rejects LLM output, falls back to rules | Proceeds with fallback parsed data |
| **Unsupported Directive** | Coerces to `no_op` | Proceeds normally |
| **`applies=false` on real rule** | Forces `applies=True` deterministically | Proceeds normally |
| **Invalid Time Window** | Coerces to `no_op` to prevent crash | Proceeds normally |

---

## ⚠️ Known Limitations

- **Rate Limiting:** The Groq API free-tier restricts concurrent requests. Under heavy concurrent load (e.g., executing 20+ back-to-back tests), the API will throw a `429 Too Many Requests` error, forcing the system to utilize the local rule-based fallback parser or incur retry latency.
- **LLM Opacity:** Highly obfuscated edge cases may bypass the LLM's comprehension, though the fallback parser mitigates total failure.
