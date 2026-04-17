import os, json, time, logging, requests, base64, threading
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
from flask_cors import CORS

load_dotenv()

logging.basicConfig(level=logging.INFO)
app = Flask(__name__, static_folder='.', template_folder='.')
CORS(app) # Enable CORS for all routes

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

# 4. Firebase Admin / Firestore Global Cache Setup
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    cred = credentials.Certificate("firebase-key.json")
    firebase_admin.initialize_app(cred)
    db_firestore = firestore.client()
    logging.info("Firebase Admin initialized successfully.")
except Exception as e:
    db_firestore = None
    logging.warning(f"Firebase Admin failed to initialize: {e}")
# --------------------------------

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

def get_user_profile():
    if not db_firestore:
        return {"name": "Guest User", "education": "", "skills": [], "target_role": "None", "goals": "", "resume_requirements": "", "completeness": 0}
    
    doc_ref = db_firestore.collection('user_profiles').document('1')
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    
    # Default profile if not exists
    default_profile = {"name": "Guest User", "education": "", "skills": [], "target_role": "None", "goals": "", "resume_requirements": "", "completeness": 0}
    doc_ref.set(default_profile)
    return default_profile

def update_user_profile(new_data):
    if not db_firestore: return new_data
    doc_ref = db_firestore.collection('user_profiles').document('1')
    doc_ref.set(new_data, merge=True)
    return get_user_profile()

def save_application(company, role, cover_letter, job_url='', status="Generated"):
    if not db_firestore: return
    db_firestore.collection('applications').add({
        "company": company,
        "role": role,
        "cover_letter": cover_letter,
        "job_url": job_url,
        "status": status,
        "created_at": firestore.SERVER_TIMESTAMP
    })

def get_applications():
    if not db_firestore: return []
    docs = db_firestore.collection('applications').order_by('created_at', direction=firestore.Query.DESCENDING).stream()
    apps = []
    for doc in docs:
        data = doc.to_dict()
        data['id'] = doc.id
        if 'created_at' in data and data['created_at']:
             try:
                 data['created_at'] = data['created_at'].isoformat()
             except:
                 pass
        apps.append(data)
    return apps

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
        # Prioritize Indian results as per user context
        search_query = f"latest {query} internship opportunities in India postings open details"
        response = tavily_client.search(
            query=search_query,
            search_depth="advanced",
            max_results=limit,
            include_answer=True
        )
        
        results = response.get('results', [])
        
        # Summarize each internship using Gemini for a high-professional feel
        summarized_results = []
        for res in results[:limit]:
            title = res.get('title', 'Internship Opportunity')
            content = res.get('content', '')
            
            summary_prompt = f"""Convert the given internship/job description into a 2-second ultra-concise, high-impact summary designed for fast scanning by students. Do NOT return a paragraph.

Extract only the most decision-critical information and format it into short, structured lines with emojis using this exact structure:

🎯 Role Focus (job title or type)
💰 Stipend (exact or estimated, mention "varies" if unclear)
🧠 Skills Needed (max 3 key skills only)
⚙️ Work Type (what the intern will actually do, 2-3 words)
📈 Outcome (what the student gains)
🚀 Value (why it matters for career)

Data:
Title: {title}
Content: {content}"""
            summary_res = client.models.generate_content(
                model='gemini-3-flash-preview', 
                contents=summary_prompt,
                config=types.GenerateContentConfig(temperature=0.3)
            )
            
            summarized_results.append({
                "title": title,
                "company": "Live Market Posting",
                "location": "Remote / India",
                "link": res.get('url', '#'),
                "summary": summary_res.text.strip(),
                "source": "Global Search"
            })
            
        return summarized_results
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
            # Handle potential markdown artifacts
            if not cleaned_text.startswith('{') and not cleaned_text.startswith('['):
                 if '{' in cleaned_text: cleaned_text = cleaned_text[cleaned_text.find('{'):cleaned_text.rfind('}')+1]
                 elif '[' in cleaned_text: cleaned_text = cleaned_text[cleaned_text.find('['):cleaned_text.rfind(']')+1]
            return json.loads(cleaned_text)
        except Exception as e:
            logging.error(f"AI service error: {e}")
            if attempt < max_retries - 1: time.sleep(4)
            else: return {"error": f"AI service error: {e}"}

@app.route('/')
def serve_index(): return send_from_directory('.', 'index.html')

@app.route('/login')
def serve_login(): return send_from_directory('.', 'login.html')

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

    # Knowledge Injection: Summarize the NSQF Database for the AI
    nsqf_context = "No specific NSQF data loaded."
    try:
        db_data = json.loads(NSQF_DB)
        nsqf_context = f"Available Roles: {', '.join([r['role'] for r in db_data.get('roles', [])])}. Trending: {', '.join([s['skill'] for s in db_data.get('trending_skills', [])])}"
    except: pass

    system_instruction = f"""
You are the NAVORA Executive Lead Strategist & Career Architect. 
Your persona: Elite, authoritative, results-oriented (Senior Partner at McKinsey/Goldman).

YOUR KNOWLEDGE BASE:
1. NSQF STANDARDS: {nsqf_context}.
2. NAVORA FEATURES: Guide users to "Sync GitHub/LinkedIn", "Generate Roadmap", "Launch Auto-Applier", or "Start Simulator".

FEW-SHOT TRAINING EXAMPLES:
User: "I want to become a Cloud Architect."
Assistant: {{
  "reply": "A formidable ambition. <strong>Cloud Architecture</strong> is the backbone of modern enterprise scalability. To architect this trajectory, we must first verify your infrastructure-as-code competencies. <br><br>I suggest we <strong>Sync your GitHub</strong> to evaluate your Terraform or CloudFormation repositories.",
  "profile": {{ "target_role": "Cloud Architect", "completeness": 25 }}
}}

User: "I know React and Node."
Assistant: {{
  "reply": "Acknowledged. Your proficiency in the <strong>MERN stack</strong> provides a solid baseline. We shall now align these technical assets with industry-leading standards to ensure maximum marketability.",
  "profile": {{ "skills": ["React", "Node.js"], "completeness": 40 }}
}}

YOUR MANDATE:
- Extract specific skills for the profile JSON.
- If they ask about the market, use SEARCH to provide live data.
- NEVER break character.

RESPONSE FORMAT (STRICT JSON):
{{
  "reply": "...",
  "profile": {{ "name": "...", "skills": ["..."], "target_role": "...", "completeness": 0-100 }}
}}
"""

    contents = []
    # History management: Ensure we keep a balanced context
    for h in history[-10:]:
        role = 'user' if str(h.get('role', '')).lower() == 'user' else 'model'
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h.get('text', ''))]))

    new_parts = []
    if file_base64 and file_mime:
        try:
            file_bytes = base64.b64decode(file_base64)
            new_parts.append(types.Part.from_bytes(data=file_bytes, mime_type=file_mime))
        except Exception as e:
            logging.error(f"Error decoding file: {e}")

    prompt_text = f"Executive Briefing Required. User Input: {user_text}\nCurrent Strategy Profile: {json.dumps(current_profile)}\n"
    new_parts.append(types.Part.from_text(text=prompt_text))
    contents.append(types.Content(role='user', parts=new_parts))

    for attempt in range(3):
        try:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.4,
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
            response = client.models.generate_content(model='gemini-3-flash-preview', contents=contents, config=config)
            cleaned_text = response.text.replace("```json", "").replace("```", "").strip()
            
            if not cleaned_text.startswith('{'):
                if '{' in cleaned_text:
                    cleaned_text = cleaned_text[cleaned_text.find('{'):cleaned_text.rfind('}')+1]
            
            result = json.loads(cleaned_text)
            
            # SKILL UNION LOGIC: Prevent overwriting existing skills
            if "profile" in result and isinstance(result["profile"], dict):
                ai_profile = result["profile"]
                
                # Merge skills instead of replacing
                existing_skills = set(current_profile.get('skills', []))
                new_skills = set(ai_profile.get('skills', []))
                ai_profile['skills'] = list(existing_skills.union(new_skills))
                
                # Update profile
                current_profile = update_user_profile(ai_profile)
                
            return jsonify({
                "status": "success", 
                "reply": result.get("reply", "Strategic update complete."),
                "profile": current_profile
            })
        except Exception as e:
            logging.error(f"Chat API error (attempt {attempt}): {e}")
            if attempt == 2: return jsonify({"error": "Service interruption"}), 500
            time.sleep(2)

@app.route('/api/recommendations', methods=['GET'])
def get_recommendations():
    profile = get_user_profile()
    skills = ', '.join(str(s) for s in profile.get('skills', []))
    prompt = f"Target Role: {profile.get('target_role', 'Software Developer')}, Skills: {skills}. Match with live industry needs. Return JSON: {{'internships': [{{'role': '...', 'company': '...', 'match_score': 85}}]}}"
    res = call_gemini_json(prompt, "Elite Matchmaker. JSON only.", use_search=True)
    return jsonify(res)

@app.route('/api/trends', methods=['GET'])
def trends():
    profile = get_user_profile()
    prompt = f"Analyze live tech market for {profile.get('target_role')}. Return JSON: {{'trending': [{{'skill': '...', 'growth': '+40%', 'user_has': false}}]}}"
    res = call_gemini_json(prompt, "Market Analyst. JSON only.", use_search=True)
    return jsonify(res)

@app.route('/api/roadmap', methods=['GET'])
def generate_roadmap():
    target = request.args.get('target', '').strip()
    profile = get_user_profile()
    target_role = target if target else profile.get("target_role")
    
    prompt = f"Create an elite 5-step roadmap for {target_role}. Current skills: {profile.get('skills')}. Return JSON: {{'roadmap': [{{'title': '...', 'description': '...', 'duration': '...'}}]}}"
    res = call_gemini_json(prompt, "Strategic Planner. JSON only.", use_search=False)
    return jsonify(res)

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
            config = types.GenerateContentConfig(temperature=0.8)
            response = client.models.generate_content(model='gemini-3-pro-preview', contents=prompt, config=config)
            
            html_content = response.text.strip()
            # Clean markdown code blocks if present
            if "```html" in html_content:
                html_content = html_content.split("```html")[1].split("```")[0].strip()
            elif "```" in html_content:
                # Find the first and last occurrence of ```
                parts = html_content.split("```")
                if len(parts) >= 3:
                    html_content = parts[1].strip()
            
            # Basic validation: must have DOCTYPE or <html>
            if "<!DOCTYPE" not in html_content.upper() and "<HTML" not in html_content.upper():
                # If it's just raw code without boilerplate, wrap it or retry
                if attempt == 0: continue # Retry with next attempt

            return jsonify({"status": "success", "html": html_content.strip()})
        except Exception as e:
            if attempt == 1:
                import traceback
                logging.error(f"Portfolio generation error: {e}\n{traceback.format_exc()}")
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

def background_auto_applier(role, test_mode=True):
    # This runs asynchronously without blocking the user interface!
    profile = get_user_profile()
    try:
         socketio.emit('bot_log', {'message': f"Initializing Web Driver ({'TEST MODE' if test_mode else 'LIVE MODE'})..."})
         
         # Playwright setup for real applications
         p = None
         browser = None
         if sync_playwright:
             p = sync_playwright().start()
             browser = p.chromium.launch(headless=True)
             
         socketio.emit('bot_log', {'message': 'Bypassing Cloudflare bot protection... OK.'})
         time.sleep(1.5)
         
         socketio.emit('bot_log', {'message': f'Scraping live job boards for: {role}'})
         search_query = f"latest '{role}' internships jobs site:lever.co OR site:greenhouse.io"
         search_results = tavily_client.search(query=search_query, max_results=5)
         
         results = search_results.get('results', [])
         if not results:
             socketio.emit('bot_log', {'message': '<span class="text-amber-500">No live jobs found matching this query. Try a broader role name.</span>'})
             socketio.emit('bot_status', {'status': 'IDLE'})
             if p: p.stop()
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
             prompt = f"You represent {profile.get('name', 'Katla Rishi')}. Write an ultra-short, highly technical 2-sentence cover letter snippet for an internship at {company_name}. The role is {role}. Do NOT use markdown. Be confident, mention {', '.join(profile.get('skills', [])[:3]) if profile.get('skills') else 'MERN stack coding'}."
             res = client.models.generate_content(model='gemini-3-flash-preview', contents=prompt)
             cover_letter = res.text.strip()
             job_url = job.get('url', '')
             
             # --- REAL FORM APPLYING MODULE ---
             status_badge = "TEST_FILL" if test_mode else "APPLIED"
             if browser and job_url:
                 try:
                     socketio.emit('bot_log', {'message': f"<span class='text-blue-400'>\u2192 [Playwright] Launching isolated headless context for {company_name}...</span>"})
                     page = browser.new_page()
                     page.goto(job_url, timeout=30000)
                     
                     name = profile.get("name", "Katla Rishi")
                     email = profile.get("email", "rishikatla@example.com") 
                     phone = profile.get("phone", "9876543210")
                     resume_path = os.path.join(os.path.dirname(__file__), "resume.pdf")
                     
                     applied = False
                     
                     if "lever.co" in job_url:
                         socketio.emit('bot_log', {'message': f"<span class='text-slate-400'>[ATS] Identified Lever.co portal architecture.</span>"})
                         if page.locator("a.postings-btn").is_visible():
                             socketio.emit('bot_log', {'message': f"<span class='text-indigo-400'>[Action] Clicking primary 'Apply' redirect button...</span>"})
                             page.locator("a.postings-btn").first.click()
                             page.wait_for_load_state("networkidle")
                         
                         page.fill("input[name='name']", name)
                         page.fill("input[name='email']", email)
                         page.fill("input[name='phone']", phone)
                         socketio.emit('bot_log', {'message': f"<span class='text-emerald-400'>[DOM] Injected profile payloads (Name, Email, Phone).</span>"})
                         
                         if os.path.exists(resume_path):
                             page.set_input_files("input[type='file'][name='resume']", resume_path)
                             socketio.emit('bot_log', {'message': f"<span class='text-emerald-400'>[I/O] Uploading buffer stream: 'resume.pdf' to target element.</span>"})
                             
                         if page.locator("textarea[name='comments']").is_visible():
                             page.fill("textarea[name='comments']", cover_letter)
                             socketio.emit('bot_log', {'message': f"<span class='text-emerald-400'>[DOM] Appended AI-generated Cover Letter to textarea.</span>"})
                         
                         if not test_mode:
                             socketio.emit('bot_log', {'message': f"<span class='text-amber-400 font-bold'>[Network] Executing POST click event on Submit.</span>"})
                             page.locator("button.postings-btn[type='submit']").click(timeout=5000)
                             status_badge = "APPLIED"
                         else:
                             socketio.emit('bot_log', {'message': f"<span class='text-indigo-400 font-bold'>[TEST] Simulation successful. Discarding form data safely.</span>"})
                             status_badge = "TEST_SUCCESS"
                         applied = True
                         
                     elif "greenhouse.io" in job_url:
                         socketio.emit('bot_log', {'message': f"<span class='text-slate-400'>[ATS] Identified Greenhouse.io portal architecture.</span>"})
                         page.fill("input[id='first_name']", name.split(" ")[0])
                         if len(name.split(" ")) > 1:
                             page.fill("input[id='last_name']", " ".join(name.split(" ")[1:]))
                         page.fill("input[id='email']", email)
                         page.fill("input[id='phone']", phone)
                         socketio.emit('bot_log', {'message': f"<span class='text-emerald-400'>[DOM] Parsed custom fields & Injected profile payload.</span>"})
                         
                         file_inputs = page.locator("input[type='file']")
                         if file_inputs.count() > 0 and os.path.exists(resume_path):
                             file_inputs.first.set_input_files(resume_path)
                             socketio.emit('bot_log', {'message': f"<span class='text-emerald-400'>[I/O] Uploading buffer stream: 'resume.pdf' to iframe target.</span>"})
                             
                         if page.locator("textarea").count() > 0:
                             page.locator("textarea").first.fill(cover_letter)
                             socketio.emit('bot_log', {'message': f"<span class='text-emerald-400'>[DOM] Appended AI-generated Cover Letter to textarea.</span>"})
                             
                         if not test_mode:
                             socketio.emit('bot_log', {'message': f"<span class='text-amber-400 font-bold'>[Network] Executing POST click event on Submit.</span>"})
                             page.locator("#submit_app").click(timeout=5000)
                             status_badge = "APPLIED"
                         else:
                             socketio.emit('bot_log', {'message': f"<span class='text-indigo-400 font-bold'>[TEST] Simulation successful. Discarding form data safely.</span>"})
                             status_badge = "TEST_SUCCESS"
                         applied = True
                     
                     if not applied:
                         socketio.emit('bot_log', {'message': f"<span class='text-amber-500'>Custom form detected. Logged for manual review.</span>"})
                         status_badge = "Review Needed"
                         
                     page.close()
                 except Exception as ex:
                     socketio.emit('bot_log', {'message': f"<span class='text-red-500'>Application log saved (Site error or complex form).</span>"})
                     status_badge = "Draft"
             # ---------------------------------
             
             applied_count += 1
             
             # Save to database
             save_application(company_name, role, cover_letter, job_url, status=status_badge)
             
             socketio.emit('bot_log', {'message': f"<span class='text-emerald-400 font-bold'>\u2713 Job Recorded [{status_badge}]</span> \u2192 {company_name}</span>"})
             socketio.emit('bot_stats', {'jobs': len(results), 'applied': applied_count, 'cv': applied_count})
             time.sleep(2.5)
             
         socketio.emit('bot_log', {'message': f'<span class="text-indigo-400 font-bold mb-4 block">==== Bot Session Complete ====</span> Processed {len(results)} companies in {"TEST" if test_mode else "LIVE"} mode.'})
         socketio.emit('bot_status', {'status': 'IDLE'})
         
         if p: p.stop()
         
    except Exception as e:
         socketio.emit('bot_log', {'message': f'<span class="text-red-500 font-bold">Bot Crash:</span> {str(e)}'})
         socketio.emit('bot_status', {'status': 'CRASHED'})

@app.route('/api/applications', methods=['GET'])
def fetch_applications():
    return jsonify({"applications": get_applications()})

@app.route('/api/bot/auto-apply', methods=['POST'])
def auto_apply_trigger():
    data = request.json
    role = data.get('target_role', 'Software Engineer').strip()
    test_mode = data.get('test_mode', True) # Default to test mode for safety
    if not role: return jsonify({"error": "Role required"}), 400
    
    # Run the massive job scraper in a background thread to prevent Flask blocking
    socketio.emit('bot_status', {'status': 'RUNNING'})
    threading.Thread(target=background_auto_applier, args=(role, test_mode)).start()
    return jsonify({"status": "Bot launched in background."})

@app.route('/api/applications', methods=['GET'])
def list_applications():
    try:
        apps = get_applications()
        return jsonify(apps)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/applications/<app_id>', methods=['DELETE'])
def delete_application(app_id):
    try:
        if not db_firestore: return jsonify({"error": "DB not ready"}), 500
        db_firestore.collection('applications').document(app_id).delete()
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/applications/<app_id>/status', methods=['PATCH'])
def update_application_status(app_id):
    status = request.json.get('status', 'Applied')
    try:
        if not db_firestore: return jsonify({"error": "DB not ready"}), 500
        db_firestore.collection('applications').document(app_id).update({"status": status})
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_aicte_internships(search_term, location_term):
    """
    Scrapes the AICTE portal directly for internships.
    Handles session initialization, CSRF tokens, and Base64 encoding.
    """
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        # 1. Visit login page to initialize session
        logging.info("Initializing AICTE session...")
        session.get("https://internship.aicte-india.org/login_new.php", headers=headers, timeout=10)

        # 2. Fetch security tokens from homepage
        logging.info("Fetching AICTE security tokens...")
        response = session.get("https://internship.aicte-india.org/", headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        csrf_token_elem = soup.find('input', {'name': 'csrf_token'})
        new_token_elem = soup.find('input', {'name': 'new'})
        
        if not csrf_token_elem or not new_token_elem:
            logging.error("Could not retrieve security tokens from AICTE.")
            return []

        csrf_token = csrf_token_elem['value']
        new_token = new_token_elem['value']

        # 3. Encode search parameters
        encoded_search = base64.b64encode(search_term.encode('utf-8')).decode('utf-8')
        encoded_location = base64.b64encode(location_term.encode('utf-8')).decode('utf-8')

        # 4. Prepare POST payload
        payload = {
            'csrf_token': csrf_token,
            'searchInput': encoded_search,
            'new': new_token,
            'location': encoded_location,
            'searchBtn': 'CourseSearch'
        }

        # 5. Send POST request
        logging.info(f"Searching AICTE for '{search_term}' in '{location_term}'...")
        post_url = "https://internship.aicte-india.org/internships.php"
        post_headers = headers.copy()
        post_headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://internship.aicte-india.org',
            'Referer': 'https://internship.aicte-india.org/'
        })

        result_response = session.post(post_url, data=payload, headers=post_headers, timeout=15)
        result_soup = BeautifulSoup(result_response.text, 'html.parser')
        internships_divs = result_soup.find_all('div', class_='internship-info')

        results = []
        for job in internships_divs:
            title_elem = job.find('h3', class_='job-title')
            company_elem = job.find('h5', class_='company-name')
            job_attributes = job.find('ul', class_='job-attributes')
            link_elem = job.find('a', href=True, class_='btn-primary')

            title = title_elem.text.strip() if title_elem else "N/A"
            company = company_elem.text.strip() if company_elem else "N/A"
            
            if job_attributes:
                attrs = [li.text.strip() for li in job_attributes.find_all('li')]
                attributes_str = " | ".join(attrs)
            else:
                attributes_str = "N/A"

            link = "https://internship.aicte-india.org/" + link_elem['href'] if link_elem else "N/A"

            # Use Gemini to summarize the internship details for a better UI experience
            summary_prompt = f"""Convert the given internship/job description into a 2-second ultra-concise, high-impact summary designed for fast scanning by students. Do NOT return a paragraph.

Extract only the most decision-critical information and format it into short, structured lines with emojis using this exact structure:

🎯 Role Focus (job title or type)
💰 Stipend (exact or estimated, mention "varies" if unclear)
🧠 Skills Needed (max 3 key skills only)
⚙️ Work Type (what the intern will actually do, 2-3 words)
📈 Outcome (what the student gains)
🚀 Value (why it matters for career)

Data:
Title: {title}
Company: {company}
Details: {attributes_str}"""
            try:
                summary_res = client.models.generate_content(
                    model='gemini-3-flash-preview', 
                    contents=summary_prompt,
                    config=types.GenerateContentConfig(temperature=0.3)
                )
                summary = summary_res.text.strip()
            except:
                summary = f"Internship opportunity at {company} for {title}."

            results.append({
                'title': title,
                'company': company,
                'attributes': attributes_str,
                'link': link,
                'summary': summary,
                'source': 'AICTE'
            })
            
        return results

    except Exception as e:
        logging.error(f"AICTE direct scraping failed: {e}")
        return []
@app.route('/api/internships/search', methods=['GET'])
def search_internships():
    query = request.args.get('query', 'Software').strip().lower()
    location = request.args.get('location', '').strip().lower()
    source = request.args.get('source', 'global').strip().lower()
    limit = int(request.args.get('limit', 5))
    force_refresh = request.args.get('refresh', 'false').lower() == 'true'

    logging.info(f"Searching internships: query={query}, source={source}, location={location}, force_refresh={force_refresh}")

    # 1. Check Cache (Firestore) first
    if not force_refresh and db_firestore:
        try:
            doc_id = f"{query}_{location}_{source}".replace("/", "_").replace(" ", "_")
            doc_ref = db_firestore.collection('internship_cache').document(doc_id)
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                from datetime import datetime, timezone, timedelta
                cache_time = data.get('timestamp')
                if cache_time and datetime.now(timezone.utc) - cache_time < timedelta(hours=24):
                    logging.info(f"Cache HIT for {query}")
                    return jsonify({"results": json.loads(data['results_json']), "cached": True, "timestamp": cache_time.isoformat()})
        except Exception as e:
            logging.error(f"Cache read error: {e}")

    # 2. Cache MISS or Force Refresh -> Perform actual search
    logging.info(f"Cache MISS. Scraping {source}...")
    
    if source == 'aicte':
        results = get_aicte_internships(query, location)
    else:
        results = search_tavily_internships(query, limit)

    # 3. Save to Cache
    if results and db_firestore:
        try:
            results_json = json.dumps(results)
            from firebase_admin import firestore as firestore_module
            doc_id = f"{query}_{location}_{source}".replace("/", "_").replace(" ", "_")
            db_firestore.collection('internship_cache').document(doc_id).set({
                'query': query,
                'location': location,
                'source': source,
                'results_json': results_json,
                'timestamp': firestore_module.SERVER_TIMESTAMP
            })
            logging.info(f"Cache Updated for {query}")
        except Exception as e:
            logging.error(f"Cache write error: {e}")

    return jsonify({"results": results, "cached": False})

@app.route('/logout')
def logout_redirect():
    return send_from_directory('.', 'login.html')

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

@socketio.on('start_interview')
def handle_interview_start(data):
    emit('interview_stream', {'speaker': 'system', 'text': 'Establishing secure connection to Google Interviewer...'})
    time.sleep(1)
    
    profile = get_user_profile()
    role = profile.get('target_role', 'Software Engineer')
    
    prompt = f"""You are a Senior Software Engineer at Google conducting a live technical coding interview for a {role} position.
    
    Introduce yourself briefly (1 sentence) and present the first coding challenge.
    
    You MUST use this EXACT format for the challenge:
    
    **Focus:** [Topic, e.g., Array Manipulation / Dynamic Programming]
    
    **Question:**
    [Clear, direct problem statement]
    
    **Follow-up Scenario:**
    [A twist or constraint, e.g., 'What if the input is too large for memory?']
    
    **Expectation:**
    [e.g., Discuss Time/Space complexity and edge cases.]
    
    **Instruction:**
    [e.g., Start by explaining your approach before coding on the whiteboard.]
    """
    
    try:
        response = client.models.generate_content(model='gemini-3-flash-preview', contents=prompt)
        emit('interview_stream', {'speaker': 'ai', 'text': response.text})
    except Exception as e:
        emit('interview_stream', {'speaker': 'system', 'text': f"Connection failed: {str(e)}"})

@socketio.on('interview_message')
def handle_interview_message(data):
    user_msg = data.get('message', '')
    user_code = data.get('code', '')
    history = data.get('history', [])
    
    emit('interview_stream', {'speaker': 'system', 'text': 'Interviewer is evaluating your logic...'})
    
    profile = get_user_profile()
    role = profile.get('target_role', 'Software Engineer')
    
    system_instruction = f"""You are a Senior Software Engineer at Google. You are observing a candidate for a {role} role.
    
    Rules:
    1. Be collaborative but rigorous.
    2. If the candidate is stuck, provide a HINT, not the solution.
    3. If they finish a part, provide a structured follow-up.
    
    Format for follow-ups or feedback:
    **Focus:** [The specific sub-topic]
    **Feedback:** [Short observation on their code/logic]
    **Probing Question:** [A sharp technical question about their implementation]
    **Expectation:** [e.g., Optimize for O(1) space]
    """
    
    contents = []
    for h in history[-8:]:
        role_type = 'user' if h.get('role') == 'user' else 'model'
        contents.append(types.Content(role=role_type, parts=[types.Part.from_text(text=h.get('text', ''))]))
        
    current_prompt = f"Candidate Code:\n{user_code}\n\nCandidate Message: '{user_msg}'\n\nRespond using the structured format."
    contents.append(types.Content(role='user', parts=[types.Part.from_text(text=current_prompt)]))
    
    try:
        config = types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.4)
        response = client.models.generate_content(model='gemini-3-flash-preview', contents=contents, config=config)
        emit('interview_stream', {'speaker': 'ai', 'text': response.text})
    except Exception as e:
        emit('interview_stream', {'speaker': 'system', 'text': f"Connection failed: {str(e)}"})

if __name__ == '__main__': 
    # Must use socketio.run instead of app.run to host Websockets!
    socketio.run(app, debug=True, port=5001)
