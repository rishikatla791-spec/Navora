# NAVORA - AI-Powered Career Assistant

NAVORA is an intelligent career management and internship discovery platform designed to help users build professional profiles, identify skill gaps, and find relevant career opportunities.

## 🚀 Features

- **AI Career Assistant:** Interactive chat-based profile builder that extracts skills, goals, and experience from conversations or uploaded resumes.
- **Skill Gap Analysis:** Compares your current profile against target roles to identify missing competencies using live market data.
- **Career Roadmaps:** Generates step-by-step personalized roadmaps to help you achieve your career goals.
- **Internship Discovery:** Integrated search for internships via Tavily and AICTE India portals.
- **Resume Generation:** Automatically generates a professional HTML-based resume tailored to your profile and specific requirements.
- **Live Tech Trends:** Provides real-time insights into trending skills and their growth in the industry.

## 🛠️ Tech Stack

- **Backend:** Flask (Python), SQLite
- **AI/LLM:** Google Gemini API (Gemini 3 Flash & Pro)
- **Search & Data:** Tavily Search API, BeautifulSoup4 (Scraping)
- **Real-time:** Flask-SocketIO (WebSockets)
- **Frontend:** HTML5, Tailwind CSS v4, Lucide Icons

## 📋 Prerequisites

- Python 3.8+
- Node.js & npm (for Tailwind CSS builds)
- Google Gemini API Key
- Tavily API Key

## ⚙️ Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/rishikatla791-spec/Navora.git
   cd Navora
   ```

2. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Install Node dependencies:**
   ```bash
   npm install
   ```

4. **Configure environment variables:**
   Create a `.env` file in the root directory and add your API keys:
   ```env
   GEMINI_API_KEY=your_gemini_api_key
   TAVILY_API_KEY=your_tavily_api_key
   ```

5. **Build Tailwind CSS:**
   ```bash
   npm run build:css
   ```

6. **Run the application:**
   ```bash
   python app.py
   ```
   The app will be available at `http://localhost:5001`.

## 📂 Project Structure

- `app.py`: Main Flask backend application.
- `index.html`: Main frontend interface.
- `input.css`: Source Tailwind CSS file.
- `output.css`: Compiled CSS file (ignored by git).
- `database.db`: Local SQLite database (ignored by git).
- `requirements.txt`: Python package dependencies.
- `package.json`: Node.js dependencies and scripts for Tailwind.

## 📄 License

This project is licensed under the ISC License.
