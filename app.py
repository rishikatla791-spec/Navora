import os, json, time, sqlite3, logging, requests, base64
from flask import Flask, jsonify, request, send_from_directory
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
app = Flask(__name__, static_folder='.', template_folder='.')
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
DATABASE = 'database.db'

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS user_profiles (id INTEGER PRIMARY KEY, profile_data TEXT)''')
        cur = db.cursor()
        cur.execute("SELECT id FROM user_profiles WHERE id = 1")
        if not cur.fetchone():
            default_profile = {"name": "Guest User", "education": "", "skills": [], "target_role": "None", "goals": "", "resume_requirements": "", "completeness": 0}
            db.execute("INSERT INTO user_profiles (id, profile_data) VALUES (1, ?)", (json.dumps(default_profile),))
        db.commit()

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
    Create a professional, clean, single-page resume formatted entirely in standard HTML (using inline CSS or generic semantic tags). DO NOT include markdown backticks (like ```html). DO NOT use external CSS classes that require a stylesheet.
    Use this profile data:
    Name: {profile.get('name', 'User')}
    Target Role: {profile.get('target_role', '')}
    Skills: {', '.join(str(s) for s in profile.get('skills', []))}
    Background/Goals: {profile.get('goals', '')}
    Education: {profile.get('education', '')}
    Specific Resume Requirements (Follow strictly): {profile.get('resume_requirements', 'None specified')}
    
    Structure the layout professionally with a header (name/role prominently displayed), a professional summary, core competencies/skills, and a section for education/goals. Keep it simple, elegant, and ready to be converted directly to a PDF.
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

if __name__ == '__main__': app.run(debug=True, port=5001)
