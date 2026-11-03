"""
Student Roster API — the server students talk to in "Exploring APIs with Bruno".

Endpoints:
    GET    /students          list every student
    GET    /students/{id}     one student
    POST   /students          add a student      body: {"name": "..."}
    PUT    /students/{id}     rename a student   body: {"name": "..."}
    DELETE /students/{id}     remove a student
    GET    /admin-only        secret data, needs header  X-API-Key: bruno-is-awesome
    GET    /docs              interactive documentation (FastAPI builds this for free)

Run it yourself:
    pip install fastapi "uvicorn[standard]"
    uvicorn roster_api:app --reload --port 5000
"""

from typing import List

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

# --- This is the hardcoded, secret API key for our server ---
API_KEY = "bruno-is-awesome"

# The roster is capped so a runaway loop can't fill the server's memory
MAX_STUDENTS = 200

app = FastAPI(title="Class Roster API")


# --- Pydantic Models for Data Validation ---
# FastAPI checks every request body against these. A missing or too-long
# name is rejected with "422 Unprocessable Content" before our code runs.

class StudentBase(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class Student(StudentBase):
    id: int


# --- In-Memory "Database" ---
# This is our "database" -- a simple list in memory.
# It disappears whenever the server restarts (or the teacher resets it).

def default_students() -> List[Student]:
    return [
        Student(id=1, name="Ada Lovelace"),
        Student(id=2, name="Grace Hopper"),
    ]


students_db = default_students()
# A counter to make sure new student IDs are unique
next_id = 3


def reset_roster():
    """Put the roster back to how it starts (used by the teacher dashboard)."""
    global students_db, next_id
    students_db = default_students()
    next_id = 3


@app.get("/")
async def home():
    # A simple welcome message for the root URL
    return {"message": "Welcome to the Student API! Visit /students to see the data or /docs for API info."}


# --- CRUD Operations ---
# These are "async def" so the server handles one at a time. With 30 students
# clicking Send at once, that guarantees no two people get the same id.

# CREATE: Add a new student
@app.post("/students", response_model=Student, status_code=status.HTTP_201_CREATED)
async def add_student(student_in: StudentBase):
    global next_id

    if len(students_db) >= MAX_STUDENTS:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="The roster is full. Ask your teacher to reset it.",
        )

    student = Student(id=next_id, name=student_in.name)
    students_db.append(student)
    next_id += 1

    # Return the new student's data
    return student


# READ: Get all students
@app.get("/students", response_model=List[Student])
async def get_students():
    return students_db


# READ: Get a single student by ID
@app.get("/students/{student_id}", response_model=Student)
async def get_student(student_id: int):
    # Find the student with the matching ID
    for student in students_db:
        if student.id == student_id:
            return student

    # If no student is found, raise a 404 "Not Found" error
    raise HTTPException(status_code=404, detail="Student not found")


# UPDATE: Modify an existing student
@app.put("/students/{student_id}", response_model=Student)
async def update_student(student_id: int, student_update: StudentBase):
    for student in students_db:
        if student.id == student_id:
            student.name = student_update.name
            return student

    # If no student is found, raise a 404
    raise HTTPException(status_code=404, detail="Student not found")


# DELETE: Remove a student
@app.delete("/students/{student_id}", status_code=status.HTTP_200_OK)
async def delete_student(student_id: int):
    global students_db

    student_found = any(s.id == student_id for s in students_db)

    if student_found:
        # Create a new list *without* the deleted student
        students_db = [s for s in students_db if s.id != student_id]
        return {"message": f"Student with id {student_id} deleted."}
    else:
        # If no student is found, raise a 404
        raise HTTPException(status_code=404, detail="Student not found")


# A secured endpoint that requires an API key
@app.get("/admin-only")
async def get_admin_data(x_api_key: str = Header(None)):
    """
    Provides secret admin data, but only if a valid
    X-API-Key header is provided.
    """
    # 1. FastAPI's Header(None) gives us the header value or None

    # 2. Check if the key is missing or incorrect
    if not x_api_key or x_api_key != API_KEY:
        # 3. If so, raise a 403 Forbidden error
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden - Invalid or missing API Key",
        )

    # 4. If the key is correct, send the secret data
    return {"message": "You found the secret admin data!"}
