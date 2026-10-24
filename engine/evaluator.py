import json
import re
from pathlib import Path
import requests
from pypdf import PdfReader

class JobEvaluator:
    def __init__(self, profile_data: dict, resume_path: Path | None, endpoint: str = "http://localhost:1234/api/v1/chat",
                 model: str = "qwen2.5-coder-7b-instruct"):
        self.profile_data = profile_data
        self.endpoint = endpoint
        self.model = model
        self.headers = {"Content-Type": "application/json"}
        self.resume_text = self._extract_resume_text(resume_path)

    def _extract_resume_text(self, resume_path: Path | None) -> str:
        """Extracts text content from the PDF resume."""
        if resume_path is None:
            return ""  # caller supplies resume text itself (e.g. coop.py)
        if not resume_path.exists():
            print(f"[Notice] Resume file not found at {resume_path}. Proceeding with profile data only.")
            return ""
        try:
            reader = PdfReader(str(resume_path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            print(f"Loaded resume successfully ({len(text)} characters extracted).")
            return text.strip()
        except Exception as e:
            print(f"[Warning] Failed to read resume PDF: {e}")
            return ""

    def _extract_text(self, res_data: dict) -> str:
        raw = res_data.get("output") or res_data.get("content")
        if not raw and "choices" in res_data:
            choice = res_data["choices"][0]
            raw = choice.get("message", {}).get("content") or choice.get("text")

        if isinstance(raw, list):
            parts = []
            for item in raw:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("text", "") or item.get("content", ""))
            return "".join(parts)
        elif isinstance(raw, str):
            return raw
        elif isinstance(raw, dict):
            return raw.get("text", "") or raw.get("content", "")
        return ""

    def query(self, system_prompt: str, user_input: str, timeout: float = 30) -> str:
        """Public entry point for other engines (e.g. screening-question answers)."""
        return self._query_model(system_prompt, user_input, timeout)

    def _query_model(self, system_prompt: str, user_input: str, timeout: float = 30) -> str:
        payload = {
            "model": self.model,
            "system_prompt": system_prompt,
            "input": user_input
        }
        try:
            response = requests.post(self.endpoint, headers=self.headers, json=payload, timeout=timeout)
            response.raise_for_status()
            res_data = response.json()
            output_text = self._extract_text(res_data)
            return re.sub(r"^```(?:json)?\s*|\s*```$", "", output_text.strip(), flags=re.MULTILINE).strip()
        except Exception as e:
            print(f"[Evaluator Error]: {e}")
            return ""

    def ask_setup_questions(self) -> str:
        system_prompt = (
            "You are an AI career assistant. Ask the user concise questions to collect: "
            "1. Target role or field, "
            "2. City/location, and "
            "3. Work commitment type (Full-time or Part-time)."
        )
        msg = self._query_model(system_prompt, "Generate opening question.")
        return msg if msg else "What job role, location, and commitment type (Part-time/Full-time) are you looking for?"

    def generate_search_matrix(self, user_intent: str, user_location: str, commitment_type: str) -> dict:
        system_prompt = (
            "You are a job search strategist. Convert user goals, target location, and commitment type "
            "into 3 to 4 concise search queries. Return ONLY a raw JSON object."
        )
        user_input = f"""
Candidate Resume & Experience:
{self.resume_text[:2500]}

Target Role: "{user_intent}"
Target Location: "{user_location}"
Commitment: "{commitment_type}"

Task:
Generate 3 to 4 realistic search query variations suitable for LinkedIn.
Map the commitment into LinkedIn job type parameters:
- 'F' for Full-Time
- 'P' for Part-Time
- '' (empty string) for Any/All

Return ONLY this format:
{{
  "location": "Ottawa, ON",
  "job_type_code": "P",
  "queries": [
    "Retail Associate",
    "Sales Associate",
    "Cashier"
  ]
}}
"""
        raw = self._query_model(system_prompt, user_input)
        try:
            parsed = json.loads(raw)
            if "queries" in parsed and isinstance(parsed["queries"], list):
                return parsed
        except Exception:
            pass

        job_code = "P" if "part" in commitment_type.lower() else ("F" if "full" in commitment_type.lower() else "")
        return {
            "location": user_location or "Canada",
            "job_type_code": job_code,
            "queries": [user_intent]
        }

    def evaluate(self, job_title: str, company: str, job_description: str, user_intent: str, commitment_type: str) -> dict:
        system_prompt = (
            "You are an expert technical and retail hiring evaluator. Compare job listings against the "
            "candidate's exact resume work history, transferable skills, and preferred schedule. "
            "Return ONLY a raw JSON object."
        )
        user_input = f"""
Candidate Resume / Work History:
{self.resume_text[:3000]}

User Preferences:
- Target Role: {user_intent}
- Preferred Schedule: {commitment_type}

Job to Screen:
- Title: {job_title}
- Company: {company}
- Description: {job_description[:2500]}

Decision Criteria:
1. MATCH: The candidate has direct or transferable background on their resume (e.g., prior retail, food service, cashier, customer care, or relevant coursework/projects).
2. SCHEDULE: Fits the requested commitment type (full-time or part-time). If the listing does not strictly specify, evaluate based on role feasibility.
3. RETAIL / CONSUMER: If seeking retail, accept store environments, cash handling, and boutique luxury sales. Reject industrial B2B distributors, wholesale cleaning chemicals, and cold-call telemarketing.
4. REJECT: Severe experience mismatch (demands 5+ years experience, managerial leadership not in candidate history, or corporate B2B).

Return ONLY this raw JSON format:
{{
  "match": true,
  "confidence": 90,
  "reason": "Detailed explanation linking listing requirements directly to candidate's previous experience."
}}
"""
        raw = self._query_model(system_prompt, user_input)
        try:
            return json.loads(raw)
        except Exception as e:
            print(f"[Evaluator JSON Error]: {e} | Raw: {raw}")
            return {"match": False, "confidence": 0, "reason": "Failed to parse evaluation response."}