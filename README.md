# Smart Campus Energy Optimizer - Production API Boilerplate

Scaffolded production-ready API skeleton with multi-stage Docker containerization and environment isolation for the **BUP CSE Fest 2026 Hackathon (In association with Poridhi)**.

---

## 📁 Repository Structure

```text
IUT_Zeroskills/
├── .dockerignore           # Excludes local artifacts, caches, and secrets from Docker builds
├── .env.example            # Template for environment variables and runtime secrets
├── .gitignore              # Strictly ignores .env, __pycache__, venvs, and secrets
├── Dockerfile              # Multi-stage, non-root, production container definition
├── requirements.txt        # FastAPI, Uvicorn, Dotenv, and Pydantic dependencies
├── README.md               # Documentation and deployment instructions
└── app/
    ├── __init__.py         # Package initialization
    ├── config.py           # Secure environment loading adhering to list data structures
    └── main.py             # FastAPI entrypoint with GET /health
```

---

## ⚙️ Core Architecture & Data Structure Rule

- **Endpoint**: `GET /health` returns HTTP 200 with `{ "status": "healthy" }` (or `{ "status": "ok" }` when `HEALTH_STATUS=ok`).
- **Secret Isolation**: No secrets or API keys are hardcoded. LLM keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) are fetched strictly at runtime from the host environment.
- **Data Structure Enforcement**: Internal groupings, configuration lookups, and route mappings strictly use lists and list-of-pairs instead of maps/dictionaries.

---

## 🚀 3-Step Deployment Checklist (Render & Railway)

### Option A: Deploying on Render (Free Tier)

#### Step 1: Create a New Web Service connected to your Git Repository
1. Log in to [Render Dashboard](https://dashboard.render.com/).
2. Click **New +** in the top right corner and select **Web Service**.
3. Select **Build and deploy from a Git repository**, click **Next**, and choose your repository (`IUT_Zeroskills`).
4. Select the branch (e.g., `chronovoid-ishuboi` or `main`).

#### Step 2: Configure the Docker Runtime
1. In the service creation form:
   - **Name**: `bup-energy-optimizer` (or your chosen name)
   - **Region**: Choose the closest region (e.g., Singapore / Frankfurt)
   - **Language / Runtime**: Choose **Docker** (Render automatically detects the root `Dockerfile`)
   - **Instance Type**: Select **Free** tier
2. Leave the Docker build command and DockerfilePath as default (`./Dockerfile`).

#### Step 3: Inject Environment Variables in the Render Dashboard
1. Scroll down to the **Environment Variables** section on the same creation page (or navigate to **Environment** in your service sidebar after creation).
2. Click **Add Environment Variable** and add:
   - `PORT` = `8000`
   - `ENVIRONMENT` = `production`
   - `HEALTH_STATUS` = `healthy` (or `ok` for BUP official judge harness evaluation)
   - `GEMINI_API_KEY` = `<your-gemini-api-key>`
   - `OPENAI_API_KEY` = `<your-openai-api-key>` (if using OpenAI)
3. Click **Create Web Service** (or **Save Changes**). Render will trigger the multi-stage Docker build, launch the container, and provide your public `https://<service-name>.onrender.com` URL. Verify with:
   ```bash
   curl https://<your-service>.onrender.com/health
   ```

---

### Option B: Deploying on Railway (Free / Hobby Tier)

#### Step 1: Initialize New Project from GitHub
1. Log in to [Railway Dashboard](https://railway.app/dashboard).
2. Click **+ New Project** -> Select **Deploy from GitHub repo**.
3. Choose your repository and select the branch `chronovoid-ishuboi` (or `main`).

#### Step 2: Auto-Detection of Dockerfile
1. Railway automatically detects `Dockerfile` in the root of the repository.
2. Click on the newly created service card and click **Deploy**.

#### Step 3: Inject Environment Variables in the Railway Dashboard
1. Click on the project service box to open the settings pane.
2. Navigate to the **Variables** tab (between *Metrics* and *Settings*).
3. Click **+ New Variable** (or **RAW Editor**) and insert your keys:
   ```env
   PORT=8000
   ENVIRONMENT=production
   HEALTH_STATUS=healthy
   GEMINI_API_KEY=your_key_here
   OPENAI_API_KEY=your_key_here
   ```
4. Navigate to the **Settings** tab -> Under **Networking**, click **Generate Domain** to get a public URL (e.g., `https://<project>.up.railway.app`).
5. Railway redeploys automatically with the new environment variables. Test the health check:
   ```bash
   curl https://<project>.up.railway.app/health
   ```

---

## 💻 Local Development

### 1. Run with Python Virtual Environment
```bash
# Create and activate virtual environment
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the API server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Test the endpoint:
```bash
curl http://localhost:8000/health
# Output: {"status":"healthy"}
```

### 2. Run with Docker
```bash
# Build multi-stage image
docker build -t energy-api:latest .

# Run container with injected environment variables (no .env baked in)
docker run -d -p 8000:8000 \
  -e PORT=8000 \
  -e HEALTH_STATUS=healthy \
  -e GEMINI_API_KEY="your-key" \
  --name energy-api-instance energy-api:latest

# Check logs
docker logs -f energy-api-instance
```
