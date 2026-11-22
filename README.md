# LLM Eval Platform ⚖️

> **An LLM-as-a-Judge evaluation platform adhering to the Vindex 1–6 Standard with 2-Pass Swap Testing for position bias detection.**

Designed & Developed with ❤️ by **[Ali Ashraf](https://github.com/Ali-Ashraf00)**

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-blue?logo=linkedin)](https://www.linkedin.com/in/ali-ashraf-89aa9b277/)
[![GitHub](https://img.shields.io/badge/GitHub-Profile-black?logo=github)](https://github.com/Ali-Ashraf00)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 🚀 Key Features

1. **Vindex 1–6 Evaluation Standard**:
   - Scores responses across 5 weighted dimensions:
     - **Instruction Following (25%)**
     - **Truthfulness (25%)**
     - **Prompt Correctness (20%)**
     - **Writing Quality (15%)**
     - **Verbosity (15%)**
2. **2-Pass Swap Test (Position Bias Detection)**:
   - Evaluates *Pass 1: A vs B* and *Pass 2: B vs A*.
   - Automatically detects whether the judge favors Position 1, ensuring statistically sound and unbiased outcomes.
3. **Multi-Engine Judge with Resilient Fallback**:
   - **Google Gemini API**: Native support for `gemini-3.8-flash` with smart auto-retry and fast preview fallback.
   - **Groq API**: High-speed Llama-3.3-70B judging.
   - **Offline Deterministic Heuristic**: Zero API key required fallback for instant local evaluation.
4. **Blind Review Mode**:
   - Rate both responses blindly across all 10 criterion points before revealing hidden AI scores to audit human-AI agreement.
5. **Zero External Dependencies**:
   - Pure Python standard library HTTP server (`urllib`, `sqlite3`, `http.server`). No complex pip setups required.
6. **Persistence & Export**:
   - Full SQLite audit history (`eval_history.db`) with one-click CSV export and history inspection.

---

## 🛠️ Tech Stack

- **Backend:** Python (Standard Library — zero dependencies)
- **Database:** SQLite3
- **Frontend:** Pure Vanilla JavaScript, HTML5, Modern CSS3
- **AI Models:** Google Gemini (`gemini-3.8-flash`), Groq (`llama-3.3-70b-versatile`)

---

## 🏁 Quick Start

### 1. Clone the repository
```bash
git clone https://github.com/Ali-Ashraf00/response-evaluator.git
cd response-evaluator
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Add your Gemini API key (or Groq API key) in `.env`:
```env
DEFAULT_PROVIDER=gemini
GEMINI_MODEL=gemini-3.8-flash
GEMINI_API_KEY=your_gemini_api_key_here
```
*(Note: If no API key is provided, the platform automatically runs in deterministic heuristic mode).*

### 3. Run the Platform

**On Windows:**
Double-click `run_app.bat` or run:
```bash
python server.py --open
```

Open your browser at `http://127.0.0.1:8000`.

---

## 📂 Project Structure

```
Response Evaluator/
├── .env.example         # Template for environment variables
├── .gitignore           # Prevents committing secrets and database
├── README.md            # Project documentation
├── rubric.json          # 5-criteria Vindex rubric definitions
├── run_app.bat          # Windows one-click launcher
├── server.py            # Zero-dependency Python backend & HTTP server
└── static/
    ├── app.js           # Frontend evaluation logic & API integration
    ├── favicon.ico      # Tab icon
    ├── index.html       # Single-page evaluation dashboard
    └── style.css        # Responsive modern UI styling
```

---

## 👨‍💻 Author

**Ali Ashraf**
- **GitHub:** [@Ali-Ashraf00](https://github.com/Ali-Ashraf00)
- **LinkedIn:** [Ali Ashraf](https://www.linkedin.com/in/ali-ashraf-89aa9b277/)
- **Email:** [mdaliashraf439@gmail.com](mailto:mdaliashraf439@gmail.com)

---

## 📄 License

This project is open-source and available under the [MIT License](LICENSE).
