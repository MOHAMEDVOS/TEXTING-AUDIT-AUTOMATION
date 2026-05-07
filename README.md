# Texting Audit Automation ✳

An advanced, high-performance automated auditing system for SMS/Texting conversations. This project uses Playwright to scrape conversations from **SmarterContact**, evaluates agent performance using **Groq AI**, and utilizes a **3-Tier ML Pre-Filter** to reduce API costs and latency.

## 🚀 Key Features

- **Automated Scraper**: High-fidelity Playwright bot designed for React/SPA architectures.
- **AI-Driven Auditing**: Full-text analysis of conversations against 4 key metrics:
  - **Compliance**: Respecting opt-outs and STOP requests.
  - **Attitude**: Judging agent sentiment and warmth.
  - **Professionalism**: Catching incoherent messages or name errors.
  - **Script Adherence**: Tracking the 3-rebuttal playbook and 4-pillar qualification.
- **ML Pre-Filter Pipeline**: A cost-saving local pipeline that skips Groq AI for "obviously clean" chats.
  - **Tier 1**: Keyword/Phrase scanning.
  - **Tier 2**: kNN Similarity matching (FAISS).
  - **Tier 3**: Logistic Regression pattern predictor.
- **Shared AI Key Pool**: LRU-based load balancing across multiple Groq API keys with automatic rotation and cooldown.
- **Performance Dashboard**: Real-time visualization of audit scores, red flags, and AI provider status.

## 🏗 Architecture

### The Three Funnels
Every conversation is classified to apply the correct rules:
1. **Wide Funnel (WF)**: Initial contact and hello.
2. **Middle Funnel (MF)**: Nurturing and initial info gathering.
3. **Narrow Funnel (NF)**: Full qualification (Hot Lead).

### The Four Pillars
For Hot Leads, the system verifies the collection of:
- **Condition**: Property state/repairs.
- **Asking Price**: Specific target number.
- **Motivation**: Why they are selling.
- **Timeline**: When they are ready to sell.

## 🛠 Tech Stack

- **Core**: Python 3.10+
- **Automation**: Playwright (Async)
- **AI Models**: Groq (Llama 3.3 70B), local scikit-learn & FAISS
- **Database**: SQLite (SQLAlchemy)
- **UI/Dashboard**: Flask / Chakra UI inspired templates

## 📋 Prerequisites

- Python 3.10 or higher
- Playwright browsers installed (`playwright install`)
- Valid Groq API keys in `config/groq_keys.json`

## 🚀 Getting Started

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Configure Environment**:
   Copy `.env.example` to `.env` and fill in your details.

3. **Run a Single Audit**:
   ```bash
   python main.py --single "AgentName"
   ```

4. **Launch Dashboard**:
   ```bash
   python dashboard/app.py
   ```

## 📈 ML Pipeline Management

To keep the local ML filters accurate, use the following commands:
- **Retrain Classifier**: `python -m ai.prefilter.train`
- **Rebuild kNN Index**: `python -m ai.prefilter.index_builder --rebuild`
- **Run Accuracy Eval**: `python scripts/eval_prefilter.py`

## 📖 Internal Documentation

For deeper dives into the system logic, see the local HTML guides:
- `docs/audit_workflow.html`: Full system overview.
- `docs/ml-prefilter-explained.html`: Deep dive into the ML tiers.
- `docs/how-the-audit-works.html`: Detailed rulebook and scoring guide.

---
**Developed by Mohamed Abdo**
