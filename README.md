# HarvesterAI
A simple tool to systematically extract data from PDFs to assist in meta-analysis and systematic reviews using AI.

The tool uses a structured set of questions, instructions, and example options to extract data from a large number of PDFs using AI, and includes a basic AI-Human collaborative data coder.

The tool is free, but requires your own API key (i.e., the program is free, but you pay your own AI computing costs). API usage costs are approximately $0.70USD per 100 PDFs using the recommended DeepSeek model, or $5.00USD per 100 PDFs using OpenAI models.

## Before Using
1. The tool does not feature title and abstract screening features. Do your title and abstract screening as you wish. I recommend ASReview, but Covidence, Rayyan, or just an excel file are all popular options.
2. Download your list of studies you wish to extract data from (e.g., for the purpose of assisted screening or for assisting in full text extraction) and find PDFs. I recommend using Zotero. Endnote may also work, but I havent tested it.
3. Make a API key and purchase credits with the AI you wish to use. 

## Steps
You can view a video of the tool is use here here https://drive.google.com/file/d/1FH6sx-Cb-1cjqXbzBMoRH1qutk5p8lvq/view?usp=drive_link

**Part 1: AI Extraction**
1. Phase your questions in the questiontemplate file. Questions may include recommended answer options (e.g., RCT, Pre-Post Intervention, Other: Insert Design Here), detailed instructions, or example answers. For simple questions these are less important, but will vastly improve the quality for complex issues. 
2. Export your Zotero or Endnote library with PDFs to a target folder and RIS formatting.
3. Open the program and fill in information about your PDF folder, Zotero library, questions, and output folder destinations.
4. Run a test run to see if the answers are satifsfactory, then either refine your questions and repeat, or continue.
5. Run the full extraction!
6. Review the answers. If you just want a nice AI table, you can stop here.
   
**Part 2: Human AI Extraction**
1. Create a list of questions for humans to answer. These will probably be similar to the AI questions, but might be more tight in formatting. For example, if needed in a specific way for a meta-analysis
2. Give the program a match between which AI answers should be suggestions for which human efforts.
3. Enter your file and folder paths.
4. Start a project, save it when done for a session, and reload it to resume. Done.


## Download
https://github.com/dphipps980/HarvesterAI/releases/ or https://drive.google.com/file/d/10laFoU_VbBnG64yZrbdMd3VLW4bp_nzH/view?usp=drive_link


## Usage tips
- When selecting an AI, models are all quite similar. I found Deepseek a good balance of cost and accuracy - https://platform.deepseek.com/ - while I have found Anthropic's models seem less prone to hallucinations but are quite costly. Either way remember that AI can and will make mistakes. Always check AI generated suggestions before publication.
- Always do a test run before the full run for AI extractions. Check for strange responses, as these often stem from some unnoticed ambiguity in the question.
- Questions should be phrased assuming nothing, especially if using the tool to assist in screening. For example "If the PDF descibes an intervention, list the target mechanisms used" will be less likely to produce halluciantions than "List the mechanisms used in the intervention"
- You can provide the tool with links in the additonal context that might help. For example "When answering this item, make your assessment using the JBI guidelines available at https://jbi.global/sites/default/files/2020-08/Checklist_for_RCTs.pdf"
- Once you start a project, avoid changing settings.
- When nominating files and folder in the Human extraction stage, use the same settings as the AI stage to reduce errors

## Notes
** Any data extracted by AI should always be human verified before publication. This tool can act as an assistant but AI can and does make mistakes.**

This project is free and developed as a hobby project/side adventure. If anything malfunctions feel free to leave feedback.
