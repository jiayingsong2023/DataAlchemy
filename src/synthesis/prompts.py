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


def get_qa_prompt(context, insights=None):
    if not insights:
        return QA_GENERATION_PROMPT.format(context=context)

    prompt = f"""
{QA_GENERATION_PROMPT}

### Numerical Insights (from Quant Agent):
{insights}

### Revised Task:
1. Extract important knowledge from the context.
2. Generate 1-3 distinct QA pairs.
3. IMPORTANT: Incorporate the numerical insights into the responses where relevant to provide deep, data-driven analysis (e.g., mention risks, trends, or correlations found by the Quant Agent).
"""
    return prompt.format(context=context)


MULTI_TURN_PROMPT = """
You are an expert AI assistant. Transform the provided context into a multi-turn conversation (2-4 turns) between a User and an AI Assistant.
The conversation should flow naturally and dive into the technical details of the context.

### Context:
{context}

### Rules:
1. Generate 2-4 turns of conversation.
2. Each turn must follow this format:
   ### User: [Question]
   ### Assistant: [Response]
3. The User should ask follow-up questions that build upon previous answers.
4. Use the same language as the context.
5. Do not include any meta-talk.
"""


def get_multi_turn_prompt(context):
    return MULTI_TURN_PROMPT.format(context=context)
