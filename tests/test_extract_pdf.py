import ast
import os
import sys
from pathlib import Path
import fitz

# Extract the extract_text_from_pdf function from app.py without executing the whole app
app_path = Path(__file__).resolve().parents[1] / "app.py"
source = app_path.read_text()
module = ast.parse(source)
func_node = None
for node in module.body:
    if isinstance(node, ast.FunctionDef) and node.name == "extract_text_from_pdf":
        func_node = node
        break
assert func_node is not None, "Function extract_text_from_pdf not found"

module_code = ast.Module([func_node], [])
code = compile(module_code, filename="app_extract", mode="exec")
namespace = {}
# Provide required globals for the function
import tempfile, pytesseract
from pdf2image import convert_from_path
namespace.update({
    'fitz': fitz,
    'convert_from_path': convert_from_path,
    'pytesseract': pytesseract,
    'tempfile': tempfile,
    'os': os,
})
exec(code, namespace)
extract_text_from_pdf = namespace['extract_text_from_pdf']

def test_extract_text_from_pdf(tmp_path):
    pdf_path = tmp_path / 'sample.pdf'
    text = 'Hello PDF World'
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        doc.save(pdf_path)

    with pdf_path.open('rb') as f:
        extracted = extract_text_from_pdf(f)

    assert text in extracted

