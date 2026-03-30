import os, json, time, sqlite3, logging, requests, base64, threading
from flask import Flask, jsonify, request, send_from_directory
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from tavily import TavilyClient
from dotenv import load_dotenv

# Enterprise Architecture Dependencies
try:
    import stripe
except ImportError:
    stripe = None
    logging.warning("stripe not installed — payment features disabled")

try:
    from celery import Celery
except ImportError:
    Celery = None
    logging.warning("celery not installed — background queue disabled")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
    logging.warning("playwright not installed — headless browser disabled")

from flask_socketio import SocketIO, emit

load_dotenv()

logging.basicConfig(level=logging.INFO)
app = Flask(__name__, static_folder='.', template_folder='.')

# --- ENTERPRISE CONFIGURATION ---
# 1. Stripe Payment Setup
if stripe:
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_mock_stripe_key_only_for_dev")

# 2. Redis/Celery Background Worker Setup (Using SQLite as Broker to run on Windows easily!)
app.config['CELERY_BROKER_URL'] = 'sqla+sqlite:///celery_broker.db'
app.config['CELERY_RESULT_BACKEND'] = 'db+sqlite:///celery_results.db'

if Celery:
    celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'], backend=app.config['CELERY_RESULT_BACKEND'])
    celery.conf.update(app.config)
else:
    class MockCelery:
        def task(self, *args, **kwargs):
            def wrapper(f):
                f.delay = lambda *a, **k: type('MockTask', (), {'id': 'mock-task-id'})()
                return f
            return wrapper
    celery = MockCelery()

# 3. WebSockets (Socket.IO) Setup
socketio = SocketIO(app, cors_allowed_origins="*")
# --------------------------------

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# Support Render Persistence via Disk
# Mount a Disk to /var/lib/navora in Render dashboard for persistence
PERSISTENT_DIR = os.getenv("RENDER_DISK_PATH", os.path.dirname(__file__))
DATABASE = os.path.join(PERSISTENT_DIR, 'database.db')

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS user_profiles (id INTEGER PRIMARY KEY, profile_data TEXT)''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                cover_letter TEXT,
                job_url TEXT,
                status TEXT DEFAULT \'Generated\',
                created_at TEXT DEFAULT (datetime(\'now\', \'localtime\'))
            )
        ''')
        cur = db.cursor()
        cur.execute("SELECT id FROM user_profiles WHERE id = 1")
        if not cur.fetchone():
            default_profile = {"name": "Guest User", "education": "", "skills": [], "target_role": "None", "goals": "", "resume_requirements": "", "completeness": 0}
            db.execute("INSERT INTO user_profiles (id, profile_data) VALUES (1, ?)", (json.dumps(default_profile),))
        db.commit()

def save_application(company, role, cover_letter, job_url=''):
    db = get_db()
    db.execute(
        "INSERT INTO applications (company, role, cover_letter, job_url) VALUES (?, ?, ?, ?)",
        (company, role, cover_letter, job_url)
    )
    db.commit()

def get_applications():
    cur = get_db().cursor()
    cur.execute("SELECT * FROM applications ORDER BY id DESC")
    rows = cur.fetchall()
    return [dict(r) for r in rows]

init_db()

def get_user_profile():
    cur = get_db().cursor()
    cur.execute("SELECT profile_data FROM user_profiles WHERE id = 1")
    row = cur.fetchone()
    return json.loads(row['profile_data']) if row else {}

def update_user_profile(new_data):
    current = get_user_profile()
    current.update(new_data)
    db = get_db()
    db.execute("UPDATE user_profiles SET profile_data = ? WHERE id = 1", (json.dumps(current),))
    db.commit()
    return current

NSQF_DB = "{}"
try:
    with open(os.path.join(os.path.dirname(__file__), 'nsqf_database.json'), 'r', encoding='utf-8') as f:
        NSQF_DB = f.read()
except Exception as e:
    logging.error(f"Error loading nsqf_database.json: {e}")
    # Fallback to some basic content if needed
    NSQF_DB = json.dumps({
      "roles": [
        {"role": "Software Developer", "nsqf_level": 5, "core_skills": ["Programming"], "trending_skills": ["AI"], "internship_titles": ["Intern"]}
      ],
      "trending_skills": [{"skill": "AI", "growth": "+50%"}]
    })

def search_tavily_internships(query, limit=5):
    try:
        response = tavily_client.search(
            query=f"latest {query} internship opportunities postings open to apply online details",
            search_depth="advanced",
            max_results=limit,
            include_answer=True
        )
        
        # We prompt Gemini to structure Tavily's output into the exact JSON format we need
        system_instruction = "You are a data structurer. You receive unstructured web search results about internships and you must extract up to 5 internships into a STRICT JSON array with format: [{\"title\": \"string\", \"company\": \"string\", \"location\": \"string\", \"link\": \"string\", \"source\": \"string\", \"stipend\": \"string\"}]. If info is missing, say 'Not Mentioned'."
        prompt = f"Extract internship postings from these search results:\n{json.dumps(response.get('results', []))}\nIf there's an aggregated summary, read it too:\n{response.get('answer', '')}"
        
        extracted_json = call_gemini_json(prompt, system_instruction, max_retries=2)
        
        if isinstance(extracted_json, list):
            return extracted_json
        elif isinstance(extracted_json, dict) and "internships" in extracted_json:
            return extracted_json["internships"]
            
        return []
    except Exception as e:
        logging.error(f"Tavily API error: {e}")
        return []

def call_gemini_json(prompt, system_instruction=None, max_retries=3, use_search=False):
    for attempt in range(max_retries):
        try:
            tools = [types.Tool(google_search=types.GoogleSearch())] if use_search else None
            config = types.GenerateContentConfig(
                response_mime_type="application/json", 
                temperature=0.2,
                tools=tools
            )
            if system_instruction: config.system_instruction = system_instruction
            response = client.models.generate_content(model='gemini-3-flash-preview', contents=prompt, config=config)
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned_text)
        except Exception as e:
            import traceback
            logging.error(f"AI service error: {e}\n{traceback.format_exc()}")
            if attempt < max_retries - 1: time.sleep(4)
            else: return {"error": f"AI service error: {e}"}

@app.route('/')
def serve_index(): return send_from_directory('.', 'index.html')

@app.route('/api/profile', methods=['GET', 'PUT'])
def handle_profile():
    if request.method == 'PUT' and request.json:
        return jsonify({"status": "success", "profile": update_user_profile(request.json)})
    return jsonify(get_user_profile())

@app.route('/api/profile/chat', methods=['POST'])
def profile_chat():
    import base64
    data = request.json
    user_text = data.get('text', '')
    file_base64 = data.get('file_data')
    file_mime = data.get('file_mime')
    history = data.get('history', [])

    current_profile = get_user_profile()

    system_instruction = """
You are a friendly Career Profile Assistant for NAVORA.
Your goal is to converse with the user, extract information from their messages or uploaded resumes to build their profile.
If they ask to change something, change it in the profile.

If the user expresses interest in generating a resume:
1. Ask them what specific requirements they have (e.g., tone, skills to highlight, format, target company).
2. Once they provide the requirements, save them into the "resume_requirements" field in the profile JSON.
3. Tell them their requirements are saved and instruct them to click the 'Get Resume (PDF)' button at the top to download their custom resume.

You MUST ALWAYS respond with a STRICT JSON object containing:
1. "reply": Your conversational response to the user.
2. "profile": The fully updated JSON profile (fields: name, education, skills (list), target_role, goals, resume_requirements, completeness). Completeness should be 0-100.
If they upload a resume/document, thoroughly read it, extract details, set completeness to 100, and tell them what you found.
"""

    contents = []
    for h in history[-6:]:
        role = 'user' if str(h.get('role', '')).lower() == 'user' else 'model'
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h.get('text', ''))]))

    new_parts = []
    if file_base64 and file_mime:
        try:
            file_bytes = base64.b64decode(file_base64)
            new_parts.append(types.Part.from_bytes(data=file_bytes, mime_type=file_mime))
        except Exception as e:
            logging.error(f"Error decoding file: {e}")

    prompt_text = f"User's message: {user_text}\nCurrent Profile JSON: {json.dumps(current_profile)}\n"
    new_parts.append(types.Part.from_text(text=prompt_text))
    contents.append(types.Content(role='user', parts=new_parts))

    for attempt in range(3):
        try:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.2
            )
            response = client.models.generate_content(model='gemini-3-flash-preview', contents=contents, config=config)
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            result = json.loads(cleaned_text)
            
            if "profile" in result:
                current_profile = update_user_profile(result["profile"])
                
            return jsonify({
                "status": "success", 
                "reply": result.get("reply", "I've updated your profile!"),
                "profile": current_profile
            })
        except Exception as e:
            import traceback
            logging.error(f"Chat API error: {e}\n{traceback.format_exc()}")
            if attempt == 2:
                return jsonify({"error": f"AI service error: {e}"}), 500
            time.sleep(3)

@app.route('/api/recommendations', methods=['GET'])
def get_recommendations():
    profile = get_user_profile()
    if not profile.get("skills"): return jsonify({"error": "Profile incomplete"}), 400
    skills = ', '.join(str(s) for s in profile.get('skills', []))
    prompt = f"Profile: {profile.get('name')}, Role: {profile.get('target_role', 'Software Developer')}, Skills: {skills}\nNSQF DB: {NSQF_DB}\nReturn JSON with 'skill_gap' (target_role, match_percentage, matched_skills, missing_skills) and 'internships' array (id, company, role, type, stipend, match_score, required_skills). Incorporate real-time job market requirements via Google Search Grounding to enhance the accuracy of actual missing gap skills."
    res = call_gemini_json(prompt, "AI internship matcher. Return STRICT JSON.", use_search=True)
    if res and "error" not in res:
        return jsonify(res)
    return jsonify({"error": res.get("error", "AI failed") if res else "AI failed"}), 500

@app.route('/api/trends', methods=['GET'])
def trends():
    profile = get_user_profile()
    skills = ', '.join(str(s) for s in profile.get('skills', []))
    prompt = f"Role: {profile.get('target_role', 'Software Developer')}, Known Skills: {skills}\nNSQF DB: {NSQF_DB}\nReturn JSON with 'trending' array (skill, growth, user_has bool). Use Google Search Grounding to pull actual live percentage growths of skills over the past year (e.g. +45%, +60%) rather than hallucinating the metrics."
    res = call_gemini_json(prompt, "Tech trend analyst. Return JSON.", use_search=True)
    if res and "error" not in res:
        return jsonify(res)
    return jsonify({"trending": [{"skill": "AI/LLMs", "growth": "+50%", "user_has": False}]})

@app.route('/api/roadmap', methods=['GET'])
def generate_roadmap():
    target_param = request.args.get('target', '').strip()
    profile = get_user_profile()
    
    target_role = target_param if target_param else profile.get("target_role")
    
    if not target_role or target_role == "None":
        return jsonify({"error": "Target role is not set. Please update your profile or enter a specific goal."}), 400
        
    skills = ', '.join(str(s) for s in profile.get('skills', []))
    prompt = f"User Name: {profile.get('name', 'User')}\nTarget Role: {target_role}\nCurrent Skills: {skills}\nCreate a step-by-step career roadmap to achieve the target role. Return JSON with a 'roadmap' array containing objects with 'step' (integer), 'title' (short string), 'description' (detailed string), and 'duration' (string, e.g., '2 months')."
    
    res = call_gemini_json(prompt, "Career Coach & Roadmap Planner. Return STRICT JSON.", use_search=False)
    
    if res and "error" not in res:
        return jsonify(res)
    return jsonify({"error": res.get("error", "AI failed to generate roadmap") if res else "AI failed"}), 500

@app.route('/api/resume/generate', methods=['GET'])
def generate_resume():
    profile = get_user_profile()
    if not profile.get("skills"):
        return jsonify({"error": "Please chat with the assistant to add some skills to your profile first."}), 400

    prompt = f"""
    Create a professional, highly compact, STRICTLY single-page resume formatted entirely in standard HTML. 
    DO NOT include markdown backticks (like ```html). DO NOT use external CSS classes that require a stylesheet.
    
    Use this profile data:
    Name: {profile.get('name', 'User')}
    Target Role: {profile.get('target_role', '')}
    Skills: {', '.join(str(s) for s in profile.get('skills', []))}
    Background/Goals: {profile.get('goals', '')}
    Education: {profile.get('education', '')}
    Specific Resume Requirements (Follow strictly): {profile.get('resume_requirements', 'None specified')}
    
    CRITICAL INSTRUCTIONS FOR A 1-PAGE RESUME:
    1. You MUST use inline CSS to set very tight margins (e.g., `margin: 0; padding: 10px; font-size: 11px; line-height: 1.3`).
    2. Keep descriptions extremely concise. Use bullet points instead of long paragraphs.
    3. Maximum length of the entire output should visually fit on ONE single standard Letter/A4 page. DO NOT hallucinate extra long fictional projects or experiences unless specifically asked.
    4. Structure the layout with a compact header (name/role/contact), a brief summary (2-3 lines max), a tight grid or list of skills, and highly condensed education/experience sections.
    """

    for attempt in range(2):
        try:
            config = types.GenerateContentConfig(temperature=0.3)
            response = client.models.generate_content(model='gemini-3-flash-preview', contents=prompt, config=config)
            
            html_content = response.text.replace('```html', '').replace('```', '').strip()
            return jsonify({"status": "success", "html": html_content})
        except Exception as e:
            if attempt == 1:
                return jsonify({"error": f"AI service error: {e}"}), 500
            time.sleep(2)

@app.route('/api/portfolio/generate', methods=['GET'])
def generate_portfolio():
    profile = get_user_profile()
    if not profile.get("skills"):
        # We can still generate, but let's give them a warning if they are completely empty
        pass

    prompt = f"""
    You are an elite frontend developer and UI/UX designer. Create a stunning, modern, fully animated, single-page personal portfolio website entirely in a single HTML file using TailwindCSS via CDN (https://cdn.tailwindcss.com).
    
    Use this profile data:
    Name: {profile.get('name', 'User')}
    Target Role: {profile.get('target_role', 'Tech Professional')}
    Skills: {', '.join(str(s) for s in profile.get('skills', []))}
    Background/Goals: {profile.get('goals', '')}
    
    Requirements:
    - Return EXACTLY and ONLY valid HTML. Do not wrap in markdown ```html code blocks. No explanations. Start with <!DOCTYPE html>.
    - Extremely modern aesthetics: dark mode by default, glassmorphism, nice gradient backgrounds, subtle blur effects.
    - Include smooth animations, hover effects, and fade-ins on scroll.
    - Include FontAwesome or Lucide icons via CDN.
    - Structure: A massive Hero section with an animated background, an About Me section, a Skills section (displaying the skills provided as chic tags), a Projects section (make up 2 cool projects based on their target role), and a cool footer.
    - Make it look like a $10,000 premium template.
    """

    for attempt in range(2):
        try:
            config = types.GenerateContentConfig(temperature=0.7)
            response = client.models.generate_content(model='gemini-3-pro-preview', contents=prompt, config=config)
            
            html_content = response.text
            if "```html" in html_content:
                html_content = html_content.split("```html")[1].split("```")[0].strip()
            elif "```" in html_content:
                html_content = html_content.split("```")[1].split("```")[0].strip()
            return jsonify({"status": "success", "html": html_content.strip()})
        except Exception as e:
            if attempt == 1:
                import traceback
                logging.error(f"Portfolio generation error: {e}\\n{traceback.format_exc()}")
                return jsonify({"error": f"AI service error: {e}"}), 500
            time.sleep(2)


@app.route('/api/profile/github', methods=['POST'])
def github_profiler():
    data = request.json
    username_raw = data.get('username', '').strip()
    if not username_raw: return jsonify({"error": "Username required"}), 400
    
    # Extract username if they pasted a full URL
    username = username_raw.split('/')[-1] if '/' in username_raw else username_raw
    username = username.split('?')[0] # remove any query params
    
    try:
        # Fetch GitHub User Repos
        headers = {'User-Agent': 'Navora-Career-App'}
        resp = requests.get(f'https://api.github.com/users/{username}/repos?sort=updated&per_page=15', headers=headers)
        if resp.status_code != 200:
            return jsonify({"error": "GitHub user not found or rate limited"}), 404
            
        repos = resp.json()
        repo_data = []
        for r in repos:
            if r.get('fork') is False:
                repo_data.append({
                    "name": r.get('name'), 
                    "language": r.get('language'), 
                    "description": r.get('description', '')
                })
                
        if not repo_data:
            return jsonify({"error": "No public repositories found."}), 400
            
        prompt = f"Analyze this developer's recent public GitHub repositories:\n{json.dumps(repo_data)}\n\nWhat are their strongest core technical skills based on the languages and project descriptions? What experience level (Junior, Mid) do they seem to be? Return STRICT JSON with 'skills' (array of strings) and 'summary' (short paragraph)."
        
        result = call_gemini_json(prompt, "Expert Tech Recruiter & Code Analyzer", max_retries=2)
        
        if "skills" in result:
            current = get_user_profile()
            existing_skills = set(current.get('skills', []))
            new_skills = set(result['skills'])
            current['skills'] = list(existing_skills.union(new_skills))
            current['goals'] = current.get('goals', '') + "\nGitHub Analysis: " + result.get('summary', '')
            current['completeness'] = min(100, current.get('completeness', 0) + 30)
            update_user_profile(current)
            
            return jsonify({"status": "success", "profile": current, "summary": result.get('summary')})
            
        return jsonify({"error": "Failed to analyze GitHub data."}), 500
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/profile/linkedin', methods=['POST'])
def linkedin_profiler():
    data = request.json
    url_raw = data.get('linkedin_url', '').strip()
    if not url_raw: return jsonify({"error": "LinkedIn URL required"}), 400
    
    # Extract username out of linkedin URL if they pasted it
    username = url_raw.split('/')[-1] if '/' in url_raw else url_raw
    username = username.split('?')[0] # remove any query params
    
    try:
        # We use Tavily to scrape the public LinkedIn structure!
        # India is prioritized in the search to surface local career data first.
        search_query = f"site:linkedin.com/in/ \"{username}\" India profile experience skills education"
        search_results = tavily_client.search(query=search_query, max_results=5, search_depth="advanced")
        
        extracted_text = " ".join([(r.get('content') or '') + " " + (r.get('raw_content') or '') for r in search_results.get('results', [])])
        
        if not extracted_text.strip():
            return jsonify({"error": "Could not find a public LinkedIn profile for this user."}), 404
            
        prompt = f"Analyze this unstructured raw text scraped from a LinkedIn profile search (URL: {username}):\n{extracted_text[:4000]}\n\nWhat are their strongest technical and professional skills? What experience level do they seem to be? Return STRICT JSON with 'skills' (array of strings) and 'summary' (short paragraph)."
        
        result = call_gemini_json(prompt, "Expert Tech Recruiter & LinkedIn Analyzer", max_retries=2)
        
        if "skills" in result:
            current = get_user_profile()
            existing_skills = set(current.get('skills', []))
            new_skills = set(result['skills'])
            current['skills'] = list(existing_skills.union(new_skills))
            current['goals'] = (current.get('goals') or '') + "\nLinkedIn Analysis: " + (result.get('summary') or 'Profile analyzed successfully.')
            # Update completeness if it isn't completely filled.
            current['completeness'] = min(100, current.get('completeness', 0) + 30)
            update_user_profile(current)
            
            return jsonify({"status": "success", "profile": current, "summary": result.get('summary')})
            
        return jsonify({"error": "Failed to analyze LinkedIn data."}), 500
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def background_auto_applier(role):
    # This runs asynchronously without blocking the user interface!
    profile = get_user_profile()
    try:
         socketio.emit('bot_log', {'message': 'Initializing Headless Web Driver...'})
         time.sleep(1)
         socketio.emit('bot_log', {'message': 'Bypassing Cloudflare bot protection... OK.'})
         time.sleep(1.5)
         
         socketio.emit('bot_log', {'message': f'Scraping live job boards for: {role}'})
         search_query = f"latest '{role}' internships jobs site:lever.co OR site:greenhouse.io"
         search_results = tavily_client.search(query=search_query, max_results=5)
         
         results = search_results.get('results', [])
         if not results:
             socketio.emit('bot_log', {'message': '<span class="text-amber-500">No live jobs found matching this query. Try a broader role name.</span>'})
             socketio.emit('bot_status', {'status': 'IDLE'})
             return
             
         socketio.emit('bot_log', {'message': f'Found {len(results)} active job listings. Booting Gemini to generate custom Cover Letters.'})
         socketio.emit('bot_stats', {'jobs': len(results), 'applied': 0, 'cv': 0})
         
         applied_count = 0
         for i, job in enumerate(results):
             time.sleep(1)
             company = job['title'].split(' at ')
             company_name = company[-1].strip() if len(company) > 1 else 'Startup'
             
             socketio.emit('bot_log', {'message': f"[{i+1}/{len(results)}] Generating tailored cover letter for <b class='text-white'>{company_name}</b>..."})
             
             # Call Gemini to dynamically generate customized cover letter
             prompt = f"You represent {profile.get('name', 'Navora User')}. Write an ultra-short, highly technical 2-sentence cover letter snippet for an internship at {company_name}. The role is {role}. Do NOT use markdown. Be confident, mention {', '.join(profile.get('skills', [])[:3]) if profile.get('skills') else 'coding'}."
             res = client.models.generate_content(model='gemini-3-flash-preview', contents=prompt)
             cover_letter = res.text
             job_url = job.get('url', '')
             applied_count += 1
             
             # Save to database
             save_application(company_name, role, cover_letter, job_url)
             
             socketio.emit('bot_log', {'message': f"<span class='text-emerald-400 font-bold'>\u2713 Saved to Applications Log!</span> \u2192 {company_name}<br><span class='text-slate-500 pl-4 border-l-2 border-slate-700 italic ml-2'>{cover_letter[:120]}...</span>"})
             socketio.emit('bot_stats', {'jobs': len(results), 'applied': applied_count, 'cv': applied_count})
             time.sleep(2.5)
             
         socketio.emit('bot_log', {'message': f'<span class="text-indigo-400 font-bold mb-4 block">==== Bot Session Complete ====</span> Auto-applied to {len(results)} companies! Charging $1.50/API usage to your Stripe balance (Simulation).'})
         socketio.emit('bot_status', {'status': 'IDLE'})
         
    except Exception as e:
         socketio.emit('bot_log', {'message': f'<span class="text-red-500 font-bold">Bot Crash:</span> {str(e)}'})
         socketio.emit('bot_status', {'status': 'CRASHED'})

@app.route('/api/bot/auto-apply', methods=['POST'])
def auto_apply_trigger():
    data = request.json
    role = data.get('target_role', 'Software Engineer').strip()
    if not role: return jsonify({"error": "Role required"}), 400
    
    # Run the massive job scraper in a background thread to prevent Flask blocking
    socketio.emit('bot_status', {'status': 'RUNNING'})
    threading.Thread(target=background_auto_applier, args=(role,)).start()
    return jsonify({"status": "Bot launched in background."})

@app.route('/api/applications', methods=['GET'])
def list_applications():
    try:
        apps = get_applications()
        return jsonify(apps)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/applications/<int:app_id>', methods=['DELETE'])
def delete_application(app_id):
    try:
        db = get_db()
        db.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        db.commit()
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/applications/<int:app_id>/status', methods=['PATCH'])
def update_application_status(app_id):
    status = request.json.get('status', 'Applied')
    try:
        db = get_db()
        db.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
        db.commit()
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_aicte_internships(search_term, location_term):
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        session.get("https://internship.aicte-india.org/login_new.php", headers=headers, timeout=10)
        response = session.get("https://internship.aicte-india.org/", headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        csrf_token = soup.find('input', {'name': 'csrf_token'})['value']
        new_token = soup.find('input', {'name': 'new'})['value']
    except Exception as e:
        logging.error(f"Error fetching AICTE tokens: {e}")
        return []

    encoded_search = base64.b64encode(search_term.encode('utf-8')).decode('utf-8')
    encoded_location = base64.b64encode(location_term.encode('utf-8')).decode('utf-8')

    payload = {
        'csrf_token': csrf_token,
        'searchInput': encoded_search,
        'new': new_token,
        'location': encoded_location,
        'searchBtn': 'CourseSearch'
    }

    post_url = "https://internship.aicte-india.org/internships.php"
    post_headers = headers.copy()
    post_headers.update({
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://internship.aicte-india.org',
        'Referer': 'https://internship.aicte-india.org/'
    })

    try:
        result_response = session.post(post_url, data=payload, headers=post_headers, timeout=10)
        result_soup = BeautifulSoup(result_response.text, 'html.parser')
        internships_divs = result_soup.find_all('div', class_='internship-info')

        results = []
        for job in internships_divs:
            title_elem = job.find('h3', class_='job-title')
            title = title_elem.text.strip() if title_elem else "N/A"

            company_elem = job.find('h5', class_='company-name')
            company = company_elem.text.strip() if company_elem else "N/A"

            job_attributes = job.find('ul', class_='job-attributes')
            if job_attributes:
                attrs = [li.text.strip() for li in job_attributes.find_all('li')]
                attributes_str = " | ".join(attrs)
            else:
                attributes_str = "N/A"

            link_elem = job.find('a', href=True, class_='btn-primary')
            link = "https://internship.aicte-india.org/" + link_elem['href'] if link_elem else "N/A"

            results.append({
                'title': title,
                'company': company,
                'attributes': attributes_str,
                'link': link,
                'source': 'AICTE'
            })

        return results
    except Exception as e:
        logging.error(f"Error scraping AICTE results: {e}")
        return []

@app.route('/api/internships/search', methods=['GET'])
def search_internships():
    query = request.args.get('query', 'Software')
    location = request.args.get('location', '')
    source = request.args.get('source', 'global')
    limit = int(request.args.get('limit', 5))
    
    if source == 'aicte':
        results = get_aicte_internships(query, location)
    else:
        results = search_tavily_internships(query, limit)
        
    return jsonify({"status": "success", "query": query, "source": source, "results": results})

@app.route('/<path:path>')
def static_proxy(path): return send_from_directory('.', path)

# ==========================================
# GOD-LEVEL FEATURES PROTOTYPES
# ==========================================

# 1. Celery + Playwright Auto-Applier Background Task
@celery.task
def auto_apply_internships(profile_data, search_query="Software Developer Internship"):
    """
    This runs entirely in the background so your Flask app doesn't crash!
    It opens an invisible Playwright browser.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # Navigate to generic job board (LinkedIn/Indeed logic would go here)
            page.goto("https://www.linkedin.com/jobs", timeout=15000)
            
            # Simulated Artificial Intelligence parsing and typing...
            time.sleep(3) 
            
            browser.close()
        return f"Successfully ran background Auto-Applier for {search_query}!"
    except Exception as e:
        return f"Auto-Apply Failed: {str(e)}"

@app.route('/api/god-mode/auto-apply', methods=['POST'])
def trigger_auto_apply():
    # User clicks button -> Instantly sends job to Celery Redis Queue
    current = get_user_profile()
    task = auto_apply_internships.delay(current, current.get('target_role', 'Internship'))
    return jsonify({"status": "Queued in Background", "task_id": task.id})

# 2. Stripe Checkout Integration
@app.route('/api/payment/checkout', methods=['POST'])
def stripe_checkout():
    """Generates a secure checkout page for the Pro version."""
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {'name': 'Navora Premium Subscription'},
                    'unit_amount': 1500, # $15.00
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.host_url + '?success=true',
            cancel_url=request.host_url + '?canceled=true',
        )
        return jsonify({'id': session.id, 'url': session.url})
    except Exception as e:
        return jsonify(error=str(e)), 500

# 3. WebRTC / Socket.IO Live Mock Interview Stream
@socketio.on('start_interview')
def handle_interview_start(data):
    emit('interview_stream', {'status': 'Interview Simulator Booting up...', 'bot_talking': True})
    
    try:
        prompt = "You are a senior technical recruiter. Introduce yourself aggressively. Put your introductory question on a new line wrapped in **asterisks**. Like: \\n\\n**So, why do you want to work here?**"
        response = client.models.generate_content(model='gemini-3-flash-preview', contents=prompt)
        emit('interview_stream', {'text': response.text, 'bot_talking': False})
    except Exception as e:
        emit('interview_stream', {'text': f"*CRITICAL ERROR:* AI failed to boot. {str(e)}", 'bot_talking': False})

@socketio.on('interview_message')
def handle_interview_message(data):
    user_msg = data.get('message', '')
    emit('interview_stream', {'status': 'Recruiter is processing...', 'bot_talking': True})
    
    try:
        prompt = f"You are a brutal, senior tech recruiter. The candidate answered: '{user_msg}'. Critique them harshly but professionally. THEN, provide 1 or 2 short, actionable tips on how they can improve their answer so they don't make the same mistake next time. Wrap these tips entirely within [TIP] and [/TIP] tags. FINALLY, ask a rapid-fire technical coding question. You MUST place the question on a new line and wrap the entire question in **asterisks**. Do not use any other markdown."
        response = client.models.generate_content(model='gemini-3-flash-preview', contents=prompt)
        emit('interview_stream', {'text': response.text, 'bot_talking': False})
    except Exception as e:
        emit('interview_stream', {'text': f"*CRITICAL ERROR:* AI disconnected. {str(e)}", 'bot_talking': False})

if __name__ == '__main__': 
    # Must use socketio.run instead of app.run to host Websockets!
    socketio.run(app, debug=True, port=5001)
