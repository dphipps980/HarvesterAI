"""
Processing module for DeepSeek PDF Extractor
Separated from GUI for better organization
"""

import os
import pdfplumber
import pandas as pd
import requests
import time
import random
import re
import concurrent.futures
from math import ceil
import threading
import traceback

lock = threading.Lock()
progress_counter = {'completed': 0, 'total': 0, 'failed': []}
stop_processing = False

# Will be set by GUI
log_callback = None
progress_callback = None
status_callback = None

def read_questions_excel(path):
    """Read questions and supporting info from Excel file."""
    try:
        df = pd.read_excel(path)
        log_callback(f"Read Excel file with {len(df)} rows", True)
        
        questions = []
        question_context = {}
        
        for idx, row in df.iterrows():
            q_num = int(row.iloc[0]) if pd.notna(row.iloc[0]) else idx + 1
            question = str(row['Question']).strip() if pd.notna(row['Question']) else ""
            
            if not question:
                continue
                
            questions.append(question)
            
            context_parts = []
            
            if pd.notna(row['Recommended Answer Options']):
                options = str(row['Recommended Answer Options']).strip()
                if options:
                    context_parts.append(options)
            
            if pd.notna(row['Additional Instructions']):
                instructions = str(row['Additional Instructions']).strip()
                if instructions:
                    context_parts.append(instructions)
            
            examples = []
            for col in ['Example Answer 1', 'Example Answer 2', 'Example Answer 3', 
                       'Example Answer 4', 'Example Answer 5']:
                if col in df.columns and pd.notna(row[col]):
                    example = str(row[col]).strip()
                    if example:
                        examples.append(example)
            
            if examples:
                context_parts.append(f"Example answers: {'; '.join(examples)}")
            
            question_context[q_num] = "\n".join(context_parts) if context_parts else ""
        
        log_callback(f"Extracted {len(questions)} questions", True)
        return questions, question_context
    except Exception as e:
        log_callback(f"Error reading Excel file: {e}\n{traceback.format_exc()}", True)
        return [], {}

def find_pdfs_recursively(folder_path):
    """Recursively find all PDF files in folder and subfolders."""
    pdf_files = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.lower().endswith('.pdf'):
                rel_path = os.path.relpath(os.path.join(root, file), folder_path)
                pdf_files.append(rel_path)
    return pdf_files

def parse_ris_file(ris_path):
    """Parse RIS file and extract bibliographic information."""
    if not ris_path or not os.path.exists(ris_path):
        return {}
    
    try:
        ris_entries = {}
        current_entry = {}
        authors = []
        pdf_files = []
        
        with open(ris_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                
                if line.startswith('TY  -'):
                    if current_entry and pdf_files:
                        if authors:
                            current_entry['authors'] = '; '.join(authors)
                        for pdf_file in pdf_files:
                            ris_entries[pdf_file] = current_entry.copy()
                    current_entry = {}
                    authors = []
                    pdf_files = []
                
                elif line.startswith('TI  -'):
                    current_entry['title'] = line[6:].strip()
                
                elif line.startswith('AU  -'):
                    authors.append(line[6:].strip())
                
                elif line.startswith('T2  -') or line.startswith('JO  -') or line.startswith('JF  -'):
                    current_entry['journal'] = line[6:].strip()
                
                elif line.startswith('PY  -') or line.startswith('Y1  -'):
                    year_text = line[6:].strip()
                    year_match = re.search(r'\d{4}', year_text)
                    if year_match:
                        current_entry['year'] = year_match.group()
                
                elif line.startswith('DO  -'):
                    current_entry['doi'] = line[6:].strip()
                
                elif line.startswith('AB  -') or line.startswith('N2  -'):
                    current_entry['abstract'] = line[6:].strip()
                
                elif line.startswith('UR  -') or line.startswith('L1  -') or line.startswith('L2  -') or line.startswith('L4  -'):
                    url = line[6:].strip()
                    
                    if url:
                        if 'file:///' in url or 'files/' in url or '\\' in url:
                            pdf_name = url.replace('file:///', '').replace('\\', '/').split('/')[-1]
                            if pdf_name.lower().endswith('.pdf'):
                                pdf_files.append(pdf_name)
                            
                            parts = url.replace('file:///', '').replace('\\', '/').split('/')
                            if len(parts) >= 3 and 'files' in parts:
                                files_idx = parts.index('files')
                                if files_idx + 2 < len(parts):
                                    zotero_path = '/'.join(parts[files_idx:])
                                    pdf_files.append(zotero_path)
                        else:
                            if url.lower().endswith('.pdf'):
                                pdf_files.append(url)
                
                elif line.startswith('ER  -'):
                    if current_entry and pdf_files:
                        if authors:
                            current_entry['authors'] = '; '.join(authors)
                        for pdf_file in pdf_files:
                            ris_entries[pdf_file] = current_entry.copy()
                    current_entry = {}
                    authors = []
                    pdf_files = []
        
        if current_entry and pdf_files:
            if authors:
                current_entry['authors'] = '; '.join(authors)
            for pdf_file in pdf_files:
                ris_entries[pdf_file] = current_entry.copy()
        
        log_callback(f"Parsed {len(ris_entries)} PDF entries from RIS file", True)
        return ris_entries
    
    except Exception as e:
        log_callback(f"Error parsing RIS file: {e}", True)
        return {}

def match_pdf_to_ris(pdf_path, ris_entries):
    """Match a PDF file path to its RIS entry."""
    pdf_filename = os.path.basename(pdf_path)
    
    if pdf_filename in ris_entries:
        return ris_entries[pdf_filename]
    
    if pdf_path in ris_entries:
        return ris_entries[pdf_path]
    
    pdf_normalized = pdf_filename.lower().replace(' ', '').replace('_', '').replace('-', '')
    
    for ris_key, ris_entry in ris_entries.items():
        ris_normalized = os.path.basename(ris_key).lower().replace(' ', '').replace('_', '').replace('-', '')
        if pdf_normalized == ris_normalized:
            return ris_entry
    
    path_parts = pdf_path.replace('\\', '/').split('/')
    if len(path_parts) >= 2:
        item_id = path_parts[-2]
        for ris_key, ris_entry in ris_entries.items():
            if item_id in ris_key:
                return ris_entry
    
    pdf_name_no_ext = os.path.splitext(pdf_filename)[0].lower()
    for ris_key, ris_entry in ris_entries.items():
        ris_name_no_ext = os.path.splitext(os.path.basename(ris_key))[0].lower()
        if pdf_name_no_ext in ris_name_no_ext or ris_name_no_ext in pdf_name_no_ext:
            if len(pdf_name_no_ext) > 5:
                return ris_entry
    
    return None

def extract_text_from_pdf(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])
            return text
    except Exception as e:
        log_callback(f"Error reading {pdf_path}: {e}", True)
        return ""

def ask_api_with_retry(api_key, pdf_text, questions, question_context, model, temperature, top_p, system_context, provider, max_retries=10):
    global stop_processing
    
    # Set URL based on provider
    if provider == 'openai':
        url = 'https://api.openai.com/v1/chat/completions'
    else:  # deepseek
        url = 'https://api.deepseek.com/chat/completions'
    
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    
    questions_text_parts = []
    for i, q in enumerate(questions, 1):
        q_text = f"{i}. {q}"
        if i in question_context and question_context[i]:
            context = question_context[i].replace('\n', ' ')
            q_text += f"\n   {context}"
        questions_text_parts.append(q_text)
    
    questions_text = "\n\n".join(questions_text_parts)
    
    user_content = (
        f"{system_context}\n\n"
        f"PDF Document:\n{pdf_text[:200000]}{'...' if len(pdf_text) > 200000 else ''}\n\n"
        f"Questions:\n{questions_text}\n\n"
        "Please provide your answers in a numbered list. "
        "For each question, start your answer with the question number in double square brackets (e.g., [[1]] for question 1), "
        "followed by your answer on the same line or next line."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": user_content}
        ],
        "temperature": temperature,
        "top_p": top_p
    }
    
    # Set max_tokens based on provider
    if provider == 'openai':
        payload["max_tokens"] = 4096  # OpenAI limit
    else:
        payload["max_tokens"] = 6000  # DeepSeek limit


    retries = 0
    while retries < max_retries and not stop_processing:
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=300)
            
            if response.status_code == 429 or "rate limit" in response.text.lower():
                wait_time = 60 + random.uniform(0, 5)
                log_callback(f"Rate limit hit. Waiting {wait_time:.1f} seconds...", True)
                time.sleep(wait_time)
                retries += 1
                continue
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.ChunkedEncodingError:
            retries += 1
            wait_time = 10 + random.uniform(0, 5)
            log_callback(f"Connection broken (attempt {retries}/{max_retries}). Waiting {wait_time:.1f}s...", True)
            time.sleep(wait_time)
            
        except requests.exceptions.ConnectionError:
            retries += 1
            wait_time = 15 + random.uniform(0, 5)
            log_callback(f"Connection error (attempt {retries}/{max_retries}). Waiting {wait_time:.1f}s...", True)
            time.sleep(wait_time)
            
        except requests.exceptions.Timeout:
            retries += 1
            wait_time = 20 + random.uniform(0, 5)
            log_callback(f"Request timeout (attempt {retries}/{max_retries}). Waiting {wait_time:.1f}s...", True)
            time.sleep(wait_time)
            
        except requests.exceptions.RequestException as e:
            retries += 1
            wait_time = 10 + random.uniform(0, 5)
            log_callback(f"Request error (attempt {retries}/{max_retries}): {e}", True)
            time.sleep(wait_time)
    
    if stop_processing:
        raise Exception("Processing stopped by user")
    
    raise Exception(f"Max retries ({max_retries}) exceeded.")

def parse_answers(response_text, num_questions):
    """Parse answers looking for [[n]] markers."""
    answers = [""] * num_questions
    
    for i in range(1, num_questions + 1):
        pattern = rf"\[\[{i}\]\]\s*(.*?)(?=\[\[\d+\]\]|$)"
        match = re.search(pattern, response_text, re.DOTALL | re.IGNORECASE)
        if match:
            answer = match.group(1).strip()
            answer = re.sub(r'\s+', ' ', answer)
            answers[i-1] = answer
    
    if all(a == "" for a in answers):
        pattern = re.compile(r"(?:^|\n)\s*\d+\.\s*")
        splits = pattern.split(response_text)
        parsed = [s.strip() for s in splits[1:] if s.strip()]
        
        for i in range(min(len(parsed), num_questions)):
            answers[i] = re.sub(r'\s+', ' ', parsed[i])
    
    return answers

def save_pdf_results(pdf_file, questions, answers, output_csv_temp):
    """Save results to CSV."""
    df_new = pd.DataFrame([{
        "PDF File": pdf_file,
        "Question": q,
        "Answer": a
    } for q, a in zip(questions, answers)])
    
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            with lock:
                if os.path.exists(output_csv_temp):
                    df_new.to_csv(output_csv_temp, mode='a', header=False, index=False, encoding='utf-8')
                else:
                    df_new.to_csv(output_csv_temp, mode='w', header=True, index=False, encoding='utf-8')
                
                progress_counter['completed'] += 1
                progress_callback(progress_counter['completed'], progress_counter['total'])
                
            return True
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(1)
            else:
                log_callback(f"FAILED to save results after {max_attempts} attempts: {e}", True)
                with lock:
                    progress_counter['completed'] += 1
                return False
    return False

def convert_to_wide_format(long_file_path, wide_file_path, ris_entries):
    """Convert long format to wide format."""
    try:
        log_callback("Converting to wide format...", True)
        
        df_long = pd.read_excel(long_file_path)
        df_long['Question_Num'] = df_long.groupby('PDF File').cumcount() + 1
        
        df_wide = df_long.pivot(
            index='PDF File',
            columns='Question_Num',
            values='Answer'
        ).reset_index()
        
        if ris_entries:
            df_wide['Title'] = df_wide['PDF File'].apply(
                lambda x: match_pdf_to_ris(x, ris_entries).get('title', '') if match_pdf_to_ris(x, ris_entries) else ''
            )
            df_wide['Authors'] = df_wide['PDF File'].apply(
                lambda x: match_pdf_to_ris(x, ris_entries).get('authors', '') if match_pdf_to_ris(x, ris_entries) else ''
            )
            df_wide['Journal'] = df_wide['PDF File'].apply(
                lambda x: match_pdf_to_ris(x, ris_entries).get('journal', '') if match_pdf_to_ris(x, ris_entries) else ''
            )
            df_wide['Year'] = df_wide['PDF File'].apply(
                lambda x: match_pdf_to_ris(x, ris_entries).get('year', '') if match_pdf_to_ris(x, ris_entries) else ''
            )
            df_wide['DOI'] = df_wide['PDF File'].apply(
                lambda x: match_pdf_to_ris(x, ris_entries).get('doi', '') if match_pdf_to_ris(x, ris_entries) else ''
            )
            df_wide['Abstract'] = df_wide['PDF File'].apply(
                lambda x: match_pdf_to_ris(x, ris_entries).get('abstract', '') if match_pdf_to_ris(x, ris_entries) else ''
            )
            
            biblio_cols = ['PDF File', 'Title', 'Authors', 'Journal', 'Year', 'DOI', 'Abstract']
        else:
            biblio_cols = ['PDF File']
        
        question_cols = [col for col in df_wide.columns if isinstance(col, int)]
        questions_map = df_long.groupby('Question_Num')['Question'].first().to_dict()
        renamed_question_cols = []
        
        for q_num in sorted(question_cols):
            question_text = questions_map.get(q_num, f"Question {q_num}")
            if len(question_text) > 50:
                question_text = question_text[:47] + "..."
            new_col_name = f"Q{q_num}: {question_text}"
            df_wide.rename(columns={q_num: new_col_name}, inplace=True)
            renamed_question_cols.append(new_col_name)
        
        df_wide = df_wide[biblio_cols + renamed_question_cols]
        
        df_wide.to_excel(wide_file_path, index=False)
        log_callback(f"Wide format saved: {len(df_wide)} rows, {len(df_wide.columns)} columns", True)
        
        if ris_entries:
            matched = df_wide['Title'].ne('').sum()
            total = len(df_wide)
            log_callback(f"RIS matching: {matched}/{total} PDFs matched", True)
        
    except Exception as e:
        log_callback(f"Error converting to wide format: {e}", True)

def process_batch(batch_id, pdf_batch, questions, question_context, api_key, pdf_folder, output_csv_temp, model, temperature, top_p, system_context, provider):
    global stop_processing
    
    for pdf_file in pdf_batch:
        if stop_processing:
            log_callback("Stopping batch processing...", True)
            break
            
        pdf_path = os.path.join(pdf_folder, pdf_file)
        log_callback(f"Batch {batch_id+1}: Processing {os.path.basename(pdf_file)}", True)
        
        try:
            pdf_text = extract_text_from_pdf(pdf_path)
            if not pdf_text:
                log_callback(f"Skipping (empty text): {os.path.basename(pdf_file)}", True)
                with lock:
                    progress_counter['failed'].append(f"{pdf_file} (empty text)")
                    progress_counter['completed'] += 1
                    progress_callback(progress_counter['completed'], progress_counter['total'])
                continue
            
            response_json = ask_api_with_retry(api_key, pdf_text, questions, question_context, 
                                              model, temperature, top_p, system_context, provider)
            
            try:
                answer_text = response_json["choices"][0]["message"]["content"]
            except Exception as e:
                log_callback(f"Error extracting answers: {e}", True)
                answer_text = ""
                with lock:
                    progress_counter['failed'].append(f"{pdf_file} (API response error)")
            
            answers = parse_answers(answer_text, len(questions))
            
            if all(a == "" for a in answers):
                log_callback(f"WARNING - No answers parsed: {os.path.basename(pdf_file)}", True)
                with lock:
                    progress_counter['failed'].append(f"{pdf_file} (no answers parsed)")
            
            success = save_pdf_results(pdf_file, questions, answers, output_csv_temp)
            if not success:
                with lock:
                    progress_counter['failed'].append(f"{pdf_file} (save failed)")
            
        except Exception as e:
            if "stopped by user" not in str(e):
                log_callback(f"ERROR processing {os.path.basename(pdf_file)}: {e}", True)
            with lock:
                progress_counter['failed'].append(f"{pdf_file} (critical error)")
                progress_counter['completed'] += 1
                progress_callback(progress_counter['completed'], progress_counter['total'])

def get_already_processed_pdfs(output_csv_temp, output_excel_long):
    """Check which PDFs have already been processed."""
    processed_pdfs = set()
    
    if os.path.exists(output_csv_temp):
        try:
            df = pd.read_csv(output_csv_temp, encoding='utf-8')
            if 'PDF File' in df.columns:
                processed_pdfs.update(df['PDF File'].unique())
                log_callback(f"Found {len(processed_pdfs)} already processed PDFs", True)
        except Exception as e:
            log_callback(f"Error reading temp CSV: {e}", True)
    
    elif os.path.exists(output_excel_long):
        try:
            df = pd.read_excel(output_excel_long)
            if 'PDF File' in df.columns:
                processed_pdfs.update(df['PDF File'].unique())
                log_callback(f"Found {len(processed_pdfs)} already processed PDFs", True)
        except Exception as e:
            log_callback(f"Error reading Excel file: {e}", True)
    
    return processed_pdfs

def process_pdfs(config, log_cb, progress_cb, status_cb):
    """Main processing function called by GUI."""
    global log_callback, progress_callback, status_callback, stop_processing
    
    log_callback = log_cb
    progress_callback = progress_cb
    status_callback = status_cb
    stop_processing = False
    
    # Set up output paths
    output_folder = config['output_folder']
    output_excel_long = os.path.join(output_folder, "DeepSeek_Results_Long.xlsx")
    output_excel_wide = os.path.join(output_folder, "DeepSeek_Results_Wide.xlsx")
    output_csv_temp = os.path.join(output_folder, "DeepSeek_Results_Temp.csv")
    
    log_callback("=== Starting Processing ===", True)
    
    # Read questions
    questions, question_context = read_questions_excel(config['questions_file'])
    if not questions:
        log_callback("ERROR: No questions found!", True)
        return
    
    # Parse RIS
    ris_entries = parse_ris_file(config['ris_file'])
    
    # Find PDFs
    all_pdf_files = find_pdfs_recursively(config['pdf_folder'])
    log_callback(f"Found {len(all_pdf_files)} PDF files", True)
    
    if not all_pdf_files:
        log_callback("ERROR: No PDF files found!", True)
        return
    
    # Check for already processed PDFs
    already_processed = get_already_processed_pdfs(output_csv_temp, output_excel_long)
    
    if already_processed:
        log_callback(f"RESUME MODE: {len(already_processed)} PDFs already processed", True)
        remaining_pdfs = [pdf for pdf in all_pdf_files if pdf not in already_processed]
        log_callback(f"Remaining PDFs to process: {len(remaining_pdfs)}", True)
        
        if not remaining_pdfs:
            log_callback("All PDFs already processed!", True)
            if os.path.exists(output_csv_temp) and not os.path.exists(output_excel_long):
                df_results = pd.read_csv(output_csv_temp, encoding='utf-8')
                df_results.to_excel(output_excel_long, index=False, engine='openpyxl')
            if os.path.exists(output_excel_long):
                convert_to_wide_format(output_excel_long, output_excel_wide, ris_entries)
            return
        
        all_pdf_files = remaining_pdfs
    
    # Select PDFs based on mode
    if config['test_mode']:
        if len(all_pdf_files) <= config['sample_size']:
            pdf_files = all_pdf_files
        else:
            pdf_files = random.sample(all_pdf_files, config['sample_size'])
        log_callback(f"TEST MODE: Processing {len(pdf_files)} random PDFs", True)
    else:
        pdf_files = all_pdf_files
        log_callback(f"FULL MODE: Processing all {len(pdf_files)} PDFs", True)
    
    # Create batches
    num_batches = min(config['max_workers'], len(pdf_files))
    batch_size = ceil(len(pdf_files) / num_batches)
    batches = [pdf_files[i*batch_size:(i+1)*batch_size] for i in range(num_batches)]
    log_callback(f"Split into {len(batches)} batches", True)
    
    # Initialize progress
    progress_counter['total'] = len(pdf_files)
    progress_counter['completed'] = 0
    progress_counter['failed'] = []
    
    # Process batches
    with concurrent.futures.ThreadPoolExecutor(max_workers=config['max_workers']) as executor:
        futures = []
        for i, batch in enumerate(batches):
            futures.append(executor.submit(
                process_batch,
                i, batch,
                questions, question_context,
                config['api_key'],
                config['pdf_folder'],
                output_csv_temp,
                config['model'],
                config['temperature'],
                config['top_p'],
                config['system_context'],
                config['provider']
            ))
        
        for future in concurrent.futures.as_completed(futures):
            if stop_processing:
                log_callback("Cancelling remaining batches...", True)
                executor.shutdown(wait=False, cancel_futures=True)
                break
    
    if stop_processing:
        log_callback("Processing stopped by user", True)
        return
    
    # Convert CSV to Excel
    try:
        if os.path.exists(output_csv_temp):
            log_callback("Converting to Excel format...", True)
            df_results = pd.read_csv(output_csv_temp, encoding='utf-8')
            df_results.to_excel(output_excel_long, index=False, engine='openpyxl')
            log_callback(f"Long format: {len(df_results)} rows", True)
            os.remove(output_csv_temp)
    except Exception as e:
        log_callback(f"Error converting CSV to Excel: {e}", True)
    
    # Print summary
    log_callback("="*60, True)
    log_callback(f"SUMMARY:", True)
    if already_processed:
        log_callback(f"Previously processed: {len(already_processed)} PDFs", True)
        log_callback(f"Newly processed: {progress_counter['completed']}/{progress_counter['total']}", True)
    else:
        log_callback(f"Total processed: {progress_counter['completed']}/{progress_counter['total']}", True)
    log_callback(f"Successful: {progress_counter['completed'] - len(progress_counter['failed'])}", True)
    log_callback(f"Failed: {len(progress_counter['failed'])}", True)
    log_callback("="*60, True)
    
    # Convert to wide format
    if os.path.exists(output_excel_long):
        convert_to_wide_format(output_excel_long, output_excel_wide, ris_entries)
    
    log_callback("Processing complete!", True)
