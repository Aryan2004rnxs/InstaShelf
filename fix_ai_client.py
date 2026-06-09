import re

with open("ai_client.py", "r") as f:
    content = f.read()

# 1. Imports
content = content.replace("import google.generativeai as genai", "from google import genai\nfrom google.genai import types")

# 2. Init
old_init = """# Initialize Gemini
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
else:
    logger.warning("GEMINI_API_KEY is not set in environment variables.")"""

new_init = """# Initialize Gemini
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = None
if GEMINI_KEY:
    gemini_client = genai.Client(api_key=GEMINI_KEY)
else:
    logger.warning("GEMINI_API_KEY is not set in environment variables.")"""
content = content.replace(old_init, new_init)

# 3. call_gemini_with_quota definition
old_def = """async def call_gemini_with_quota(model, contents: List[Any], generation_config: Dict[str, Any] = None) -> str:
    \"\"\"
    Wrapper for model.generate_content that checks and tracks the 20 daily quota.
    Raises RuntimeError if quota is exceeded.
    \"\"\"
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")
        
    usage = get_gemini_usage()
    if usage >= 20:
        logger.warning(f"Gemini API quota exceeded for today ({usage}/20).")
        raise RuntimeError("Gemini Quota Limit Exceeded")
        
    increment_gemini_usage()
    
    # Run the Gemini API call in a thread pool since it's blocking
    loop = asyncio.get_running_loop()
    if generation_config:
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                contents,
                generation_config=generation_config,
                request_options=genai.types.RequestOptions(timeout=90.0)
            )
        )
    else:
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                contents,
                request_options=genai.types.RequestOptions(timeout=90.0)
            )
        )
        
    return response.text"""

new_def = """async def call_gemini_with_quota(model_name: str, contents: List[Any], system_instruction: str = None, json_mode: bool = False, temperature: float = None) -> str:
    \"\"\"
    Wrapper for model.generate_content that checks and tracks the 20 daily quota.
    Raises RuntimeError if quota is exceeded.
    \"\"\"
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY is not set.")
        
    usage = get_gemini_usage()
    if usage >= 20:
        logger.warning(f"Gemini API quota exceeded for today ({usage}/20).")
        raise RuntimeError("Gemini Quota Limit Exceeded")
        
    increment_gemini_usage()
    
    config_kwargs = {}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"
    if temperature is not None:
        config_kwargs["temperature"] = temperature
        
    # The new SDK takes a GenerateContentConfig object
    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
    
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: gemini_client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config
        )
    )
        
    return response.text"""

content = content.replace(old_def, new_def)

# 4. Usages of call_gemini_with_quota
# A. Multimodal Vision
old_mm_vision = """                        model = genai.GenerativeModel(
                            model_name=GEMINI_MODEL_NAME,
                            system_instruction=SYSTEM_PROMPT
                        )
                        
                        prompt = prompt_template
                        if raw_text:
                            prompt += f"\\n\\nTEXT:\\n{raw_text}"
                            
                        contents = pil_images + [prompt]
                        
                        response_text = await call_gemini_with_quota(
                            model,
                            contents,
                            generation_config={"response_mime_type": "application/json"}
                        )"""

new_mm_vision = """                        prompt = prompt_template
                        if raw_text:
                            prompt += f"\\n\\nTEXT:\\n{raw_text}"
                            
                        contents = pil_images + [prompt]
                        
                        response_text = await call_gemini_with_quota(
                            GEMINI_MODEL_NAME,
                            contents,
                            system_instruction=SYSTEM_PROMPT,
                            json_mode=True
                        )"""
content = content.replace(old_mm_vision, new_mm_vision)

# B. Text-only
old_txt1 = """            model = genai.GenerativeModel(
                model_name=GEMINI_MODEL_NAME,
                system_instruction=SYSTEM_PROMPT
            )
            response_text = await call_gemini_with_quota(
                model,
                [prompt],
                generation_config={"response_mime_type": "application/json"}
            )"""

new_txt1 = """            response_text = await call_gemini_with_quota(
                GEMINI_MODEL_NAME,
                [prompt],
                system_instruction=SYSTEM_PROMPT,
                json_mode=True
            )"""
content = content.replace(old_txt1, new_txt1)

old_txt2 = """                model = genai.GenerativeModel(
                    model_name=GEMINI_MODEL_NAME,
                    system_instruction=SYSTEM_PROMPT
                )
                response_text = await call_gemini_with_quota(
                    model,
                    [prompt + "\\nIMPORTANT: respond in JSON only!"],
                    generation_config={
                        "response_mime_type": "application/json",
                        "temperature": 0.0
                    }
                )"""

new_txt2 = """                response_text = await call_gemini_with_quota(
                    GEMINI_MODEL_NAME,
                    [prompt + "\\nIMPORTANT: respond in JSON only!"],
                    system_instruction=SYSTEM_PROMPT,
                    json_mode=True,
                    temperature=0.0
                )"""
content = content.replace(old_txt2, new_txt2)

# C. Dedups
old_dedup1 = """            model = genai.GenerativeModel(model_name=GEMINI_MODEL_NAME)
            response_text = await call_gemini_with_quota(
                model,
                [prompt],
                generation_config={"response_mime_type": "application/json"}
            )"""

new_dedup1 = """            response_text = await call_gemini_with_quota(
                GEMINI_MODEL_NAME,
                [prompt],
                json_mode=True
            )"""
content = content.replace(old_dedup1, new_dedup1) # replaces both dedup functions

# D. Simple text responses
old_simple1 = """            model = genai.GenerativeModel(model_name=GEMINI_MODEL_NAME)
            response_text = await call_gemini_with_quota(model, [prompt])"""

new_simple1 = """            response_text = await call_gemini_with_quota(GEMINI_MODEL_NAME, [prompt])"""
content = content.replace(old_simple1, new_simple1)

with open("ai_client.py", "w") as f:
    f.write(content)

print("Done")
