import os
import sys
import json
import re
import uuid
import sqlite3
import argparse
import random
from typing import Dict, Any, List

# LangChain Imports
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_ollama import ChatOllama

# FastAPI Imports
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_NAME = "college_assistant.db"

# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT NOT NULL,
            semester INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            steps TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()

def get_or_create_student(student_id: str, name: str, department: str = None, semester: int = None) -> dict:
    student_id = student_id.strip().upper()
    name = name.strip()
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM students WHERE student_id = ?", (student_id,))
    row = c.fetchone()
    
    if row:
        if row[1].strip().lower() != name.lower():
            conn.close()
            raise ValueError("Student ID already exists with a different name. Please use a different ID.")
        student = {"student_id": row[0], "name": row[1], "department": row[2], "semester": row[3]}
    else:
        if not department:
            depts = ["Computer Science", "Information Technology", "Electronics", "Mechanical Engineering", "Civil Engineering"]
            dept = random.choice(depts)
        else:
            dept = department.strip()
        sem = semester if semester is not None else random.randint(1, 8)
        c.execute("INSERT INTO students VALUES (?, ?, ?, ?)", (student_id, name, dept, sem))
        conn.commit()
        student = {"student_id": student_id, "name": name, "department": dept, "semester": sem}
        
    conn.close()
    return student

def save_message(chat_id: str, role: str, content: str, steps: list = None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    steps_str = json.dumps(steps) if steps else None
    c.execute("INSERT INTO messages (chat_id, role, content, steps) VALUES (?, ?, ?, ?)",
              (chat_id, role, content, steps_str))
    conn.commit()
    conn.close()

def get_chat_messages(chat_id: str) -> list:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content, steps, timestamp FROM messages WHERE chat_id = ? ORDER BY message_id ASC", (chat_id,))
    rows = c.fetchall()
    conn.close()
    
    messages = []
    for r in rows:
        messages.append({
            "role": r[0],
            "content": r[1],
            "steps": json.loads(r[2]) if r[2] else [],
            "timestamp": r[3]
        })
    return messages

def create_new_chat(student_id: str, title: str) -> dict:
    chat_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO chats (chat_id, student_id, title) VALUES (?, ?, ?)", (chat_id, student_id, title))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id, "student_id": student_id, "title": title}

def delete_chat(chat_id: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def get_student_chats(student_id: str) -> list:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id, student_id, title, created_at FROM chats WHERE student_id = ? ORDER BY created_at DESC", (student_id,))
    rows = c.fetchall()
    conn.close()
    
    chats = []
    for r in rows:
        chats.append({
            "chat_id": r[0],
            "student_id": r[1],
            "title": r[2],
            "created_at": r[3]
        })
    return chats

# ─────────────────────────────────────────────
# STUDENT DATABASE (COMPATIBILITY LOOKUP DICT)
# ─────────────────────────────────────────────
student_db = {
    "ST101": {"name": "Lathika", "department": "Computer Science", "semester": 5},
    "ST102": {"name": "Pavindhar", "department": "Electronics", "semester": 3},
    "ST103": {"name": "Nandana", "department": "Information Technology", "semester": 7}
}

# ─────────────────────────────────────────────
# TOOL 1 – ATTENDANCE CALCULATOR
# ─────────────────────────────────────────────
@tool
def check_attendance(total_classes: int, attended_classes: int) -> dict:
    """
    Calculate attendance percentage and exam eligibility.

    Args:
        total_classes: Total number of classes held.
        attended_classes: Number of classes attended by the student.

    Returns:
        A dict with attendance_percent and exam_status.
    """
    percentage = (attended_classes / total_classes) * 100
    status = "Eligible for Exam" if percentage >= 75 else "Not Eligible for Exam"
    return {
        "attendance_percent": round(percentage, 2),
        "exam_status": status
    }

# ─────────────────────────────────────────────
# TOOL 2 – VIT GPA CALCULATOR
# ─────────────────────────────────────────────
@tool
def calculate_gpa(grades: List[str], credits: List[int]) -> dict:
    """
    Calculate GPA (Grade Point Average) based on VIT University grade points and course credits.
    
    Args:
        grades: List of letter grades (S, A, B, C, D, E, F, N).
        credits: List of credits allocated to each course.
        
    Returns:
        A dict with gpa and total_credits.
    """
    if not grades or not credits or len(grades) != len(credits):
        return {"error": "Grades and credits lists must be of equal size."}
        
    mapping = {
        "S": 10,
        "A": 9,
        "B": 8,
        "C": 7,
        "D": 6,
        "E": 5,
        "F": 0,
        "N": 0
    }
    
    total_points = 0.0
    total_credits = 0
    
    for g, c in zip(grades, credits):
        gp = mapping.get(g.strip().upper(), 0)
        total_points += gp * c
        total_credits += c
        
    gpa = total_points / total_credits if total_credits > 0 else 0.0
    return {
        "gpa": round(gpa, 2),
        "total_credits": total_credits
    }

# ─────────────────────────────────────────────
# TOOL 3 – VIT CGPA CALCULATOR
# ─────────────────────────────────────────────
@tool
def calculate_cgpa(semester_gpas: List[float], semester_credits: List[int]) -> dict:
    """
    Calculate CGPA (Cumulative Grade Point Average) based on credit-weighted completed semesters.
    
    Args:
        semester_gpas: List of GPAs obtained in completed semesters.
        semester_credits: List of total credits earned in each completed semester.
        
    Returns:
        A dict with cgpa and total_cumulative_credits.
    """
    if not semester_gpas or not semester_credits or len(semester_gpas) != len(semester_credits):
        return {"error": "Semester GPAs and credits lists must be of equal size."}
        
    total_points = 0.0
    total_credits = 0
    
    for gpa, c in zip(semester_gpas, semester_credits):
        total_points += gpa * c
        total_credits += c
        
    cgpa = total_points / total_credits if total_credits > 0 else 0.0
    return {
        "cgpa": round(cgpa, 2),
        "total_cumulative_credits": total_credits
    }

# ─────────────────────────────────────────────
# TOOL 4 – FEE BALANCE CALCULATOR
# ─────────────────────────────────────────────
@tool
def calculate_fee_balance(total_course_fee: float, amount_paid: float) -> dict:
    """
    Calculate the pending course fee amount.

    Args:
        total_course_fee: Total fee for the course.
        amount_paid: Amount already paid by the student.

    Returns:
        A dict with pending_fee.
    """
    pending_fee = total_course_fee - amount_paid
    return {"pending_fee": pending_fee}

# ─────────────────────────────────────────────
# TOOL 5 – LIBRARY FINE CALCULATOR
# ─────────────────────────────────────────────
@tool
def calculate_library_fine(delayed_days: int, daily_rate: float = 5.0) -> dict:
    """
    Calculate library fine based on number of delayed days.
    Fine = daily_rate × delayed_days.

    Args:
        delayed_days: Number of days the book was returned late.
        daily_rate: Daily fine rate in rupees (default is 5.0).

    Returns:
        A dict with fine_amount.
    """
    fine_amount = delayed_days * daily_rate
    return {"fine_amount": fine_amount}

# ─────────────────────────────────────────────
# TOOL 6 – HOSTEL FEE CALCULATOR
# ─────────────────────────────────────────────
@tool
def calculate_hostel_charges(monthly_hostel_fee: float, months_stayed: int) -> dict:
    """
    Calculate total hostel fee based on monthly fee and duration.

    Args:
        monthly_hostel_fee: Fee charged per month for hostel.
        months_stayed: Number of months the student stayed.

    Returns:
        A dict with total_hostel_fee.
    """
    total_fee = monthly_hostel_fee * months_stayed
    return {"total_hostel_fee": total_fee}

# ─────────────────────────────────────────────
# BONUS TOOL – STUDENT INFORMATION LOOKUP
# ─────────────────────────────────────────────
@tool
def get_student_record(student_id: str):
    """
    Retrieve student information using a Student ID.

    Args:
        student_id: The unique student identifier (e.g., ST101).

    Returns:
        Student details dict or a not-found message.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT name, department, semester FROM students WHERE student_id = ?", (student_id.upper(),))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {"name": row[0], "department": row[1], "semester": row[2]}
    
    if student_id in student_db:
        return student_db[student_id]
    return "Student Record Not Found"

# ─────────────────────────────────────────────
# TOOLS LIST
# ─────────────────────────────────────────────
tools = [
    check_attendance,
    calculate_gpa,
    calculate_cgpa,
    calculate_fee_balance,
    calculate_library_fine,
    calculate_hostel_charges,
    get_student_record
]

# ─────────────────────────────────────────────
# DYNAMIC MULTI-LLM BACKEND SELECTION
# ─────────────────────────────────────────────
llm = None

# 1. Try OpenAI if key is present
if os.getenv("OPENAI_API_KEY"):
    try:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
        print("Backend: Initialized OpenAI GPT-4o-Mini (Cloud Accelerated).")
    except Exception as err:
        print(f"Warning: Failed to load OpenAI model ({err}).")

# 2. Try Gemini if key is present
if llm is None and os.getenv("GEMINI_API_KEY"):
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)
        print("Backend: Initialized Google Gemini 1.5 Flash (Cloud Accelerated).")
    except Exception as err:
        print(f"Warning: Failed to load Gemini model ({err}).")

# 3. Fallback to local Ollama qwen2.5
if llm is None:
    print("Backend: Falling back to local Ollama (qwen2.5).")
    llm = ChatOllama(
        model="qwen2.5",
        temperature=0
    )

# ─────────────────────────────────────────────
# PROMPT TEMPLATE
# ─────────────────────────────────────────────
prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
You are SmartCollegeBot, an intelligent assistant for college students.

Your responsibilities:
- Attendance Management
- GPA & CGPA Calculation (using VIT credit-weighted criteria)
- Fee Management
- Hostel Fee Calculation
- Library Fine Calculation
- Student Information Retrieval

Instructions:
- Carefully understand the student's request.
- If the request needs one tool, call only that tool.
- If the request requires multiple calculations, invoke multiple tools
  and provide a well-structured summary report.
- Keep responses neat, clear, and professional.
- Always include the ₹ symbol for monetary values.
- Return structured answers, using Markdown tables or lists for reports where appropriate.
        """
    ),
    ("human", "{input}"),
    ("placeholder", "{agent_scratchpad}")
])

# ─────────────────────────────────────────────
# CREATE AGENT & EXECUTOR
# ─────────────────────────────────────────────
agent = create_tool_calling_agent(
    llm=llm,
    tools=tools,
    prompt=prompt
)

agent_executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    return_intermediate_steps=True
)

# ─────────────────────────────────────────────
# FASTAPI APP SETUP
# ─────────────────────────────────────────────
app = FastAPI(title="Smart College Assistant Enterprise API", description="API server for agentic college assistance")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str
    chat_id: str

class AuthRequest(BaseModel):
    name: str
    student_id: str
    department: str
    semester: int

@app.on_event("startup")
def startup_event():
    init_db()

# High-Speed Calculator Router (Under 2ms response time)
def check_fast_path(query: str) -> dict:
    steps = []
    output_parts = []
    
    # 1. Attendance Calculator
    attendance_match = re.search(r'(?:attend(?:ed)?\s+(\d+)\s+classes\s+out\s+of\s+(\d+))|(?:total\s+(\d+).*attended\s+(\d+))|(?:attended\s+(\d+).*total\s+(\d+))|(\d+)\s+out\s+of\s+(\d+)', query, re.IGNORECASE)
    if attendance_match:
        groups = [g for g in attendance_match.groups() if g is not None]
        if len(groups) == 2:
            if "out of" in query.lower() or "/" in query.lower() or query.lower().find("attend") < query.lower().find("total"):
                attended, total = int(groups[0]), int(groups[1])
            else:
                total, attended = int(groups[0]), int(groups[1])
            
            if total > 0:
                res = check_attendance.invoke({"total_classes": total, "attended_classes": attended})
                steps.append({
                    "tool": "check_attendance",
                    "input": {"total_classes": total, "attended_classes": attended},
                    "output": res
                })
                output_parts.append(
                    f"### <i class='fa-solid fa-calendar-check text-teal-600 mr-2'></i>Attendance Status\n"
                    f"- **Total Lectures:** {total}\n"
                    f"- **Lectures Attended:** {attended}\n"
                    f"- **Attendance Rate:** **{res['attendance_percent']}%**\n"
                    f"- **Exam Eligibility:** <span class='px-2 py-0.5 rounded text-xs font-semibold "
                    f"{'bg-emerald-100 text-emerald-800' if 'Eligible' in res['exam_status'] and 'Not' not in res['exam_status'] else 'bg-rose-100 text-rose-800'}'>"
                    f"{res['exam_status']}</span>"
                )

    # 2. GPA Calculator (VIT Method)
    # Example: "Calculate GPA for grades ["S","A","B","A","S"] with credits [4,4,3,4,2]"
    gpa_match = re.search(r'gpa.*grades?\s*\[([\w"\'\s,]+)\].*credits?\s*\[([\d,\s]+)\]', query, re.IGNORECASE)
    if gpa_match:
        try:
            grades_str = gpa_match.group(1)
            grades = [g.replace('"', '').replace("'", "").strip().upper() for g in re.findall(r'[a-zA-Z]+', grades_str)]
            
            credits_str = gpa_match.group(2)
            credits_list = [int(c.strip()) for c in re.findall(r'\d+', credits_str)]
            
            res = calculate_gpa.invoke({"grades": grades, "credits": credits_list})
            steps.append({
                "tool": "calculate_gpa",
                "input": {"grades": grades, "credits": credits_list},
                "output": res
            })
            output_parts.append(
                f"### <i class='fa-solid fa-graduation-cap text-teal-600 mr-2'></i>GPA Calculation Report (VIT Method)\n"
                f"- **Course Grades:** {grades}\n"
                f"- **Subject Credits:** {credits_list}\n"
                f"- **Total Credits Registered:** {res['total_credits']}\n"
                f"- **Calculated GPA Score:** <span class='font-bold text-teal-600 text-lg'>{res['gpa']}</span> / 10.0"
            )
        except Exception as e:
            print(f"Fast-path GPA error: {e}")

    # 3. CGPA Calculator (VIT Credit Weighted)
    # Example: "Calculate CGPA for semester GPAs [8.72, 8.95, 9.10] with semester credits [21, 24, 28]"
    cgpa_match = re.search(r'cgpa.*gpas?\s*\[([\d\.\s,]+)\].*credits?\s*\[([\d\s,]+)\]', query, re.IGNORECASE)
    if cgpa_match:
        try:
            gpas_str = cgpa_match.group(1)
            gpas = [float(g.strip()) for g in re.findall(r'\d+(?:\.\d+)?', gpas_str)]
            
            credits_str = cgpa_match.group(2)
            credits_list = [int(c.strip()) for c in re.findall(r'\d+', credits_str)]
            
            res = calculate_cgpa.invoke({"semester_gpas": gpas, "semester_credits": credits_list})
            steps.append({
                "tool": "calculate_cgpa",
                "input": {"semester_gpas": gpas, "semester_credits": credits_list},
                "output": res
            })
            output_parts.append(
                f"### <i class='fa-solid fa-chart-line text-teal-600 mr-2'></i>CGPA Calculation Report (VIT Credit Weighted)\n"
                f"- **Semester GPAs:** {gpas}\n"
                f"- **Semester Credits:** {credits_list}\n"
                f"- **Total Cumulative Credits:** {res['total_cumulative_credits']}\n"
                f"- **Cumulative CGPA Score:** <span class='font-bold text-teal-600 text-lg'>{res['cgpa']}</span> / 10.0"
            )
        except Exception as e:
            print(f"Fast-path CGPA error: {e}")

    # 4. Fee Balance Calculator
    fee_match = False
    if any(word in query.lower() for word in ["fee", "paid", "pending", "balance", "remit"]):
        fee_nums = [float(n) for n in re.findall(r'\d+', query)]
        if len(fee_nums) >= 2:
            total = max(fee_nums[0], fee_nums[1])
            paid = min(fee_nums[0], fee_nums[1])
            fee_match = True
            
    if fee_match:
        res = calculate_fee_balance.invoke({"total_course_fee": total, "amount_paid": paid})
        steps.append({
            "tool": "calculate_fee_balance",
            "input": {"total_course_fee": total, "amount_paid": paid},
            "output": res
        })
        output_parts.append(
            f"### <i class='fa-solid fa-credit-card text-teal-600 mr-2'></i>Course Fee Summary\n"
            f"- **Total Course Fee:** ₹{total:,.2f}\n"
            f"- **Amount Already Paid:** ₹{paid:,.2f}\n"
            f"- **Pending Fee Balance:** <span class='font-semibold text-rose-600'>₹{res['pending_fee']:,.2f}</span>"
        )

    # 5. Library Fine Calculator
    fine_match = re.search(r'(\d+)\s+day', query, re.IGNORECASE)
    if fine_match and any(word in query.lower() for word in ["library", "fine", "book", "late"]):
        days = int(fine_match.group(1))
        
        rate_match = re.search(r'(?:rate.*₹?\s*(\d+))|(?:₹\s*(\d+)\s*per\s*day)', query, re.IGNORECASE)
        rate = 5.0
        if rate_match:
            r_groups = [rg for rg in rate_match.groups() if rg is not None]
            if r_groups:
                rate = float(r_groups[0])
                
        res = calculate_library_fine.invoke({"delayed_days": days, "daily_rate": rate})
        steps.append({
            "tool": "calculate_library_fine",
            "input": {"delayed_days": days, "daily_rate": rate},
            "output": res
        })
        output_parts.append(
            f"### <i class='fa-solid fa-book text-teal-600 mr-2'></i>Library Fine Details\n"
            f"- **Late Return Delay:** {days} days\n"
            f"- **Fine Rate:** ₹{rate:.2f} / day\n"
            f"- **Total Library Fine:** <span class='font-bold text-rose-600'>₹{res['fine_amount']:.2f}</span>"
        )

    # 6. Hostel Fee Calculator
    hostel_match = False
    if "hostel" in query.lower():
        hostel_nums = [int(n) for n in re.findall(r'\d+', query)]
        if len(hostel_nums) >= 2:
            months = min(hostel_nums[0], hostel_nums[1])
            monthly_fee = max(hostel_nums[0], hostel_nums[1])
            if months < 24:
                hostel_match = True

    if hostel_match:
        res = calculate_hostel_charges.invoke({"monthly_hostel_fee": monthly_fee, "months_stayed": months})
        steps.append({
            "tool": "calculate_hostel_charges",
            "input": {"monthly_hostel_fee": monthly_fee, "months_stayed": months},
            "output": res
        })
        output_parts.append(
            f"### <i class='fa-solid fa-house-chimney text-teal-600 mr-2'></i>Hostel Charges Statement\n"
            f"- **Monthly Hostel Fee:** ₹{monthly_fee:,.2f}\n"
            f"- **Months Stayed:** {months} months\n"
            f"- **Total Hostel Fee:** **₹{res['total_hostel_fee']:,.2f}**"
        )

    # 7. Student Information Lookup
    id_match = re.search(r'\bST\d{3}\b', query, re.IGNORECASE)
    if id_match:
        student_id = id_match.group(0).upper()
        res = get_student_record.invoke({"student_id": student_id})
        steps.append({
            "tool": "get_student_record",
            "input": {"student_id": student_id},
            "output": res
        })
        if isinstance(res, dict):
            output_parts.append(
                f"### <i class='fa-solid fa-id-card text-teal-600 mr-2'></i>Student Profile (ID: {student_id})\n"
                f"- **Name:** **{res['name']}**\n"
                f"- **Department:** {res['department']}\n"
                f"- **Semester:** Semester {res['semester']}"
            )
        else:
            output_parts.append(
                f"### <i class='fa-solid fa-id-card text-teal-600 mr-2'></i>Student Profile (ID: {student_id})\n"
                f"- **Status:** *{res}*"
            )

    if not steps:
        return None
        
    consolidated_output = "\n\n".join(output_parts)
    prefix = (
        "> [!NOTE]\n"
        "> **Optimized Route:** Query executed instantly via high-speed database router.\n\n"
    )
    return {
        "output": prefix + consolidated_output,
        "intermediate_steps": steps,
        "mode": "optimized_fastpath"
    }

# Rule-based fallback simulator in case Ollama is offline
def simulated_agent_fallback(user_query: str) -> dict:
    res = check_fast_path(user_query)
    if res:
        res["output"] = res["output"].replace("Optimized Route:", "Offline Mode Fallback:")
        res["mode"] = "fallback_simulated"
        return res
        
    return {
        "output": "> [!NOTE]\n> **Offline Mode:** Running calculations locally.\n\nI could not identify any specific queries in your message. Please ask me about attendance, results, fees, library fines, hostel charges, or look up student records.",
        "intermediate_steps": [],
        "mode": "fallback_simulated"
    }

# ─────────────────────────────────────────────
# API CONTROLLERS
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def read_root():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h3>index.html not found. Please create the frontend in the root directory.</h3>"

@app.post("/api/auth/signin")
async def sign_in(auth: AuthRequest):
    if not auth.student_id or not auth.name:
        raise HTTPException(status_code=400, detail="Student ID and Name are required")
    try:
        student = get_or_create_student(auth.student_id, auth.name, auth.department, auth.semester)
        return student
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/students")
async def get_all_students():
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT student_id, name, department, semester FROM students ORDER BY student_id ASC")
        rows = c.fetchall()
        conn.close()
        return [{"student_id": r[0], "name": r[1], "department": r[2], "semester": r[3]} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/api/chats")
async def get_chats(request: Request):
    student_id = request.headers.get("X-Student-Id")
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    chats = get_student_chats(student_id)
    return chats

@app.post("/api/chats")
async def create_chat(request: Request, body: dict):
    student_id = request.headers.get("X-Student-Id")
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    title = body.get("title", "New Chat Query")
    new_chat = create_new_chat(student_id, title)
    return new_chat

@app.delete("/api/chats/{chat_id}")
async def delete_chat_endpoint(chat_id: str, request: Request):
    student_id = request.headers.get("X-Student-Id")
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    delete_chat(chat_id)
    return {"status": "success"}

@app.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: str, request: Request):
    student_id = request.headers.get("X-Student-Id")
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    messages = get_chat_messages(chat_id)
    return messages

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest, req: Request):
    student_id = req.headers.get("X-Student-Id")
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthorized: Please sign in first")
        
    query = request.message.strip()
    chat_id = request.chat_id.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")
        
    # Save user message to database
    save_message(chat_id, "user", query, None)
    
    # Try optimized fast-path routing first for instant responses
    fast_path_result = check_fast_path(query)
    if fast_path_result:
        save_message(chat_id, "assistant", fast_path_result["output"], fast_path_result["intermediate_steps"])
        return fast_path_result
        
    # Otherwise call the LangChain dynamic agent
    try:
        response = agent_executor.invoke({"input": query})
        
        steps_taken = []
        if "intermediate_steps" in response:
            for action, observation in response["intermediate_steps"]:
                steps_taken.append({
                    "tool": action.tool,
                    "input": action.tool_input,
                    "output": observation
                })
                
        save_message(chat_id, "assistant", response["output"], steps_taken)
        return {
            "output": response["output"],
            "intermediate_steps": steps_taken,
            "mode": "langchain_agent"
        }
    except Exception as e:
        print(f"LangChain execution error: {e}. Falling back to simulator.", file=sys.stderr)
        fallback_res = simulated_agent_fallback(query)
        save_message(chat_id, "assistant", fallback_res["output"], fallback_res["intermediate_steps"])
        return fallback_res

@app.get("/api/student/{student_id}")
async def get_student_api(student_id: str):
    res = get_student_record.invoke({"student_id": student_id.upper()})
    if res == "Student Record Not Found":
        raise HTTPException(status_code=404, detail="Student Record Not Found")
    return res

# ─────────────────────────────────────────────
# CLI RUNNER FOR COMPATIBILITY
# ─────────────────────────────────────────────
def run_cli_mode():
    init_db()
    print("""
    
            SMART COLLEGE ASSISTANT (CLI MODE)
            SARATHY P – 23MID0094              
    
    Available Services:
      1. Attendance Calculator
      2. GPA Calculator (VIT Method)
      3. CGPA Calculator (VIT Method)
      4. Fee Balance Calculator
      5. Library Fine Calculator
      6. Hostel Fee Calculator
      7. Student Information Lookup
    
    Type 'exit' to quit.
    """)
    
    while True:
        user_query = input("\nAsk Your Question : ").strip()
    
        if not user_query:
            continue
    
        if user_query.lower() == "exit":
            print("\nThank you for using Smart College Assistant.")
            break
    
        try:
            # Try fast path
            fast = check_fast_path(user_query)
            if fast:
                print("\nASSISTANT RESPONSE (FAST ROUTE)\n")
                print(fast["output"])
                print("\n")
                continue
                
            try:
                response = agent_executor.invoke({"input": user_query})
                print("\nASSISTANT RESPONSE\n")
                print(response["output"])
                print("\n")
            except Exception as e:
                fallback = simulated_agent_fallback(user_query)
                print("\nASSISTANT RESPONSE (FALLBACK)\n")
                print(fallback["output"])
                print("\n")
        except Exception as e:
            print(f"\nError: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smart College Assistant Server")
    parser.add_argument("--cli", action="store_true", help="Run in Command Line interface mode")
    parser.add_argument("--port", type=int, default=8000, help="Web server port")
    args = parser.parse_args()
    
    if args.cli:
        run_cli_mode()
    else:
        import uvicorn
        import os
        print("Pre-initializing database...")
        init_db()
        # Bind to PORT environment variable for Render deployment, fallback to argparse
        bind_port = int(os.environ.get("PORT", args.port))
        reload_mode = False if os.environ.get("PORT") else True
        print(f"Starting Smart College Assistant Enterprise Web Server on port {bind_port}...")
        uvicorn.run("Smart_College_Assistant:app", host="0.0.0.0", port=bind_port, reload=reload_mode)
