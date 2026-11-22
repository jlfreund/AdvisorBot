# LLM Configuration & Key Management Guide

AdvisorBot supports multiple simultaneous LLM providers for direct chat, interactive analysis, and automated scenario audits. You can configure any combination of cloud APIs, free tier keys, paid pay-as-you-go keys, and local zero-cost models.

---

## 1. Quick Start: Where to Put Configuration Files

AdvisorBot automatically discovers LLM configuration files across two directory tiers:

| Tier | Directory Path | Scope | Precedence |
| :--- | :--- | :--- | :--- |
| **Portfolio-Specific** | `Portfolios/<PortfolioName>/LLM/*.json` | Applies only when `<PortfolioName>` is active | **Highest** (overrides global duplicate IDs) |
| **Global Default** | `Data/Config/LLM/*.json` | Applies to all portfolios as fallback | **Fallback** |
| **Environment Variables** | `~/.zshrc` (macOS), `~/.bashrc` (Linux), or User Env Vars (Windows) | `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, etc. | Used if `api_key` in JSON is empty |

To activate a provider:
1. Choose an example template from `Examples/` (e.g., `llm_config.gemini.json`, `llm_config.claude.json`, `llm_config.openai.json`, or `llm_config.local_ollama.json`).
2. Copy it into `Data/Config/LLM/` (for global use) or `Portfolios/<YourPortfolio>/LLM/` (for portfolio-specific use):
   - **macOS / Linux**:
     ```bash
     mkdir -p Data/Config/LLM
     # Example: configure Gemini globally
     cp Examples/llm_config.gemini.json Data/Config/LLM/gemini.json

     # Example: configure Claude for a specific portfolio
     mkdir -p Portfolios/my_household/LLM
     cp Examples/llm_config.claude.json Portfolios/my_household/LLM/claude.json
     ```
   - **Windows (PowerShell)**:
     ```powershell
     New-Item -ItemType Directory -Force -Path Data\Config\LLM | Out-Null
     # Example: configure Gemini globally
     Copy-Item Examples\llm_config.gemini.json Data\Config\LLM\gemini.json

     # Example: configure Claude for a specific portfolio
     New-Item -ItemType Directory -Force -Path Portfolios\my_household\LLM | Out-Null
     Copy-Item Examples\llm_config.claude.json Portfolios\my_household\LLM\claude.json
     ```
3. Open the file and replace `PASTE_YOUR_..._KEY_HERE` with your key, or set the corresponding environment variable.

---

## 2. Provider Details & Key Types

### A. Google Gemini

- **Config File**: `Data/Config/LLM/gemini.json`
- **Template**: [llm_config.gemini.json](llm_config.gemini.json)
- **Supported Key Types**:
  1. **Paid Pay-As-You-Go API Key (Recommended)**:
     - Created in [Google AI Studio](https://aistudio.google.com/apikey) linked to a Google Cloud Billing account.
     - Offers high Requests Per Minute (RPM) and Tokens Per Minute (TPM) without a daily request cap.
  2. **Free-Tier API Key**:
     - Keys without a billing account attached operate under Google AI Studio Free Tier rules.
     - **Important Limitation**: Free Tier allows only **20 requests per day** on flagship models like `gemini-2.5-flash`. Once 20 calls are made within a 24-hour window, the API returns `HTTP 429: Generate content requests per day limit exceeded on Free Tier`.
     - *Tip*: If using a free key, select `Gemini 2.5 Flash-Lite` (`gemini-2.5-flash-lite`), which has significantly higher free-tier limits.
  3. **External CLI Agents (`agy`, Claude Code, Gemini CLI)**:
     - External CLI coding agents use their own terminal credentials and connect via the opt-in `holdings-tools` MCP server (`Scripts/mcp_server.py`), **not** the in-app AI Chat drawer (which connects directly to your configured HTTP LLM APIs under Privacy Shield enforcement and blocks CLI subprocess engines by default).
  4. **Google Cloud Vertex AI**:
     - Update `"api_base_url"` to your regional Vertex AI endpoint and supply a service account bearer token or Gateway API key.

---

### B. Anthropic Claude

- **Config File**: `Data/Config/LLM/claude.json`
- **Template**: [llm_config.claude.json](llm_config.claude.json)
- **Supported Key Types**:
  1. **Prepaid API Key (Starts with `sk-ant-api03-`)**:
     - Created in the [Anthropic Console](https://console.anthropic.com/settings/keys).
     - **Important Limitation**: Anthropic **does not provide a free tier API**. You must deposit prepaid credits under [Billing Settings](https://console.anthropic.com/settings/billing). A key with a $0 credit balance will fail with credits exhausted.
  2. **Claude Enterprise / Bedrock / Vertex**:
     - Custom Anthropic endpoints can be configured by pointing `"api_base_url"` to your AWS Bedrock or GCP Vertex Anthropic proxy.

---

### C. OpenAI

- **Config File**: `Data/Config/LLM/openai.json`
- **Template**: [llm_config.openai.json](llm_config.openai.json)
- **Supported Key Types**:
  1. **Project API Key (Starts with `sk-proj-`) (Recommended)**:
     - Created in the [OpenAI Platform](https://platform.openai.com/api-keys). Scoped to a specific project.
  2. **Legacy User Key (Starts with `sk-`)**:
     - Account-wide key.
  3. **Credit Balance Requirement**:
     - OpenAI API usage is billed separately from consumer ChatGPT Plus subscriptions. You must add prepaid credits under [Organization Billing](https://platform.openai.com/settings/organization/billing). Keys without credits return `You have no credits remaining` (HTTP 429).
  4. **Azure OpenAI**:
     - Change `"api_base_url"` to:
       `https://<your-resource>.openai.azure.com/openai/deployments/<deployment>/chat/completions?api-version=2024-02-15-preview`
     - Paste your Azure resource key into `"api_key"`.

---

### D. Local & Self-Hosted Models (Ollama, LM Studio, LocalAI, vLLM)

- **Config File**: `Data/Config/LLM/ollama.json`
- **Template**: [llm_config.local_ollama.json](llm_config.local_ollama.json)
- **Zero Cost & 100% Private**:
  - Run completely offline without internet connectivity, API subscriptions, or rate limits.
  - Setup:
    1. Install [Ollama](https://ollama.com/):
       - **macOS**: `brew install ollama`
       - **Linux**: `curl -fsSL https://ollama.com/install.sh | sh`
       - **Windows**: `winget install Ollama.Ollama`
    2. Download models: `ollama pull llama3.3` or `ollama pull qwen2.5:14b`.
    3. Start server: `ollama serve` (listens on `http://localhost:11434`).
    4. Copy `Examples/llm_config.local_ollama.json` to `Data/Config/LLM/ollama.json` (`Data\Config\LLM\ollama.json` on Windows).
  - **Loopback Privacy**: Because loopback endpoints (`127.0.0.1`, `localhost`) do not transmit data over the external internet, AdvisorBot permits them under strict **Local-Only Privacy Mode**.

---

## 3. Environment Variable Fallback

If you prefer not to store plaintext API keys in JSON files on disk:
1. Leave `"api_key": ""` or `"PASTE_YOUR_..._KEY_HERE"` in the JSON configuration.
2. Set your keys in your environment for your platform:
   - **macOS** (`~/.zshrc`) / **Linux** (`~/.bashrc`):
     ```bash
     export GEMINI_API_KEY="AIzaSy..."
     export ANTHROPIC_API_KEY="sk-ant-api03-..."
     export OPENAI_API_KEY="sk-proj-..."
     ```
   - **Windows** (PowerShell — persistent user variables):
     ```powershell
     [Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "AIzaSy...", "User")
     [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-api03-...", "User")
     [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "sk-proj-...", "User")
     ```
3. When AdvisorBot starts, it checks for these environment variables automatically whenever a JSON key is unpopulated.

---

## 4. Understanding UI Status Indicators

In the AI Chat header and the Portfolio Badge model selector, each model displays a live status capsule:

| Indicator | Meaning | Cause & Remedy |
| :--- | :--- | :--- |
| `🟢 Model (742ms)` | **Ready & Verified** | Live probe successfully contacted the provider endpoint within the shown roundtrip latency. |
| `⚪ Model (needs key)` | **Configured but Inactive** | The config JSON file exists, but `api_key` has not been pasted yet and no environment variable was found. |
| `🔴 Model (quota/429)` | **Rate or Daily Limit** | Google AI Studio free tier limit reached (20/day limit) or enterprise rate limit exceeded. Upgrade key to pay-as-you-go or switch to a lite model. |
| `🔴 Model (no credits)` | **Billing Credits Exhausted** | OpenAI or Anthropic account has an active key but a $0 credit balance. Deposit funds in provider billing settings. |
| `🔴 Model (invalid key)` | **Authentication Error** | Key string is malformed, revoked, or lacks permission for the requested model. |
| `🚫 Model (blocked)` | **Local-Only Endpoint Restriction** | Portfolio Privacy Mode is set to **Local-Only**, which restricts external cloud endpoints. Use a local model (Ollama) or adjust Portfolio Settings -> Privacy. |
