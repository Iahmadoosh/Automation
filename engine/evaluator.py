import json
import re
import requests

class JobEvaluator:
    def __init__(self, profile_data: dict, endpoint: str = "http://localhost:1234/api/v1/chat"):
        self.profile_data = profile_data
        self.endpoint = endpoint
        self.headers = {"Content-Type": "application/json"}

    def _query_model(self, system_prompt: str, user_input: str) -> str:
        payload = {
            "model": "qwen2.5-coder-7b-instruct",
            "system_prompt": system_prompt,
            "input": user_input
        }
        try:
            response = requests.post(self.endpoint, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            res_data = response.json()

            output_text = res_data.get("output") or res_data.get("content") or ""
            if not output_text and "choices" in res_data:
                output_text = res_data["choices"][0]["message"]["content"]

            return re.sub(r"^```(?:json)?\s*|\s*```$", "", output_text.strip(), flags=re.MULTILINE)
        except Exception as e:
            print(f"[Evaluator Error]: {e}")
            return "{}"

    def parse_search_intent(self, user_prompt: str) -> dict:
        """
        Translates a natural language request (e.g., 'find me a retail job')
        into optimized search keywords and location parameters.
        """
        system_prompt = (
            "You are a job search strategist. Convert user requests into standard "
            "LinkedIn search terms based on their profile. Return ONLY a valid JSON object."
        )
        user_input = f"""
Candidate Profile:
{json.dumps(self.profile_data, indent=2)}

User Request:
"{user_prompt}"

Task:
Extract or formulate the best short search keyword query (1-3 words) and location for LinkedIn.
Example output:
{{
  "keywords": "Retail Sales Associate",
  "location": "Canada"
}}
"""
        raw = self._query_model(system_prompt, user_input)
        try:
            return json.loads(raw)
        except Exception:
            return {"keywords": user_prompt, "location": "Canada"}

    def evaluate(self, job_title: str, company: str, job_description: str, user_intent: str) -> dict:
        """
        Evaluates whether a listing fits both candidate profile AND the requested job intent.
        """
        system_prompt = (
            "You are an automated job screener. You decide if a listing matches what the user "
            "wants and if they are qualified. Return ONLY a valid JSON object."
        )
        user_input = f"""
Candidate Profile:
{json.dumps(self.profile_data, indent=2)}

User's Desired Search:
"{user_intent}"

Job Posting:
- Title: {job_title}
- Company: {company}
- Description: {job_description[:2000]}

Task:
Evaluate whether this position matches the user's intent and background.
Return ONLY this JSON format:
{{
  "match": true,
  "confidence": 85,
  "reason": "Clear explanation of fit or mismatch."
}}
"""
        raw = self._query_model(system_prompt, user_input)
        try:
            return json.loads(raw)
        except Exception:
            return {"match": False, "confidence": 0, "reason": "Failed to parse evaluation response."}