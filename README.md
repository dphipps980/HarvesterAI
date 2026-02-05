# HarvesterAI
A simple tool to systematically extract data from PDFs to assist in meta-analysis and systematic reviews using AI.

The tool uses a structured set of questions, instructions, and example options to extract data from a large number of PDFs using AI.

The tool is free, but requires your own API key (i.e., the program is free, but you pay your own AI computing costs). API usage costs are approximately $0.70USD per 100 PDFs using the recommended DeepSeek model, or $5.00USD per 100 PDFs using OpenAI models.

Before Using:
1. Do your title and abstract screening as you wish. For example using ASReview or Covidence.
2. Download your list of studies you wish to extract data from (e.g., for the purpose of assisted screening or for assisting in full text extraction) and find PDFs. I recommend using Zotero or Endnote.
3. Make a API key and purchase credits with the AI you wish to use. I use Deepseek as I have found it a good balance of cost and accuracy - https://platform.deepseek.com/  

Steps:
1. Phase your questions in the questiontemplate file. Questions may include recommended answer options (e.g., RCT, Pre-Post Intervention, Other: Insert Design Here), detailed instructions, or example answers. For simple questions these are less important, but will vastly improve the quality for complex issues. 
2. Export your Zotero or Endnote library with PDFs to a target folder and RIS formatting.
3. Open the program and fill in information about your PDF folder, Zotero library, questions, and output folder destinations.
4. Run a test run to see if the answers are satifsfactory, then either refine your questions and repeat, or continue.
5. Run the full extraction! 


Download: https://github.com/dphipps980/HarvesterAI/releases/


## Usage tips
- Always do a test run before the full run.
- Questions should be phrased assuming nothing, especially if using the tool to assist in screening. For example "If the PDF descibes an intervention, list the target mechanisms used" will be less likely to produce halluciantions than "List the mechanisms used in the intervention"
- You can provide the tool with links in the additonal context that might help. For example "When answering this item, make your assessment using the JBI guidelines available at https://jbi.global/sites/default/files/2020-08/Checklist_for_RCTs.pdf"

## Notes
- Any data extracted by AI should always be human verified before publication. This tool can act as an assistant but AI can and does make mistakes.
