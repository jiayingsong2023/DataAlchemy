QA_GENERATION_PROMPT = """
You are an expert AI assistant specialized in creating high-quality SFT (Supervised Fine-Tuning) data.
Your goal is to transform the provided context into a set of high-quality "Instruction-Response" pairs.

### Context:
{context}

### Task:
1. Extract the most important technical knowledge, facts, or procedures from the context.
2. Generate 1-3 distinct QA pairs.
3. Each pair must follow this format:
   ### Instruction: [The question or task]
   ### Response: [The detailed, accurate answer based on the context]

### Rules:
- The answer must be strictly based on the provided context.
- Avoid generic questions. Be specific to the technical details in the text.
- If the context is code, focus on its functionality, usage, or potential bugs it fixes.
- Use the same language as the context (e.g., if the context is in Chinese, generate QA in Chinese).
- Do not include any meta-talk, only the QA pairs.
"""

def get_qa_prompt(context):
    return QA_GENERATION_PROMPT.format(context=context)
