import re

with open('style.css', 'r', encoding='utf-8') as f:
    css = f.read()

open_braces = css.count('{')
close_braces = css.count('}')
print(f'Open braces: {open_braces}, Close braces: {close_braces}')
if open_braces != close_braces:
    print('ERROR: Unbalanced braces!')
else:
    print('Braces are balanced')

lines = css.split('\n')
for i, line in enumerate(lines, 1):
    stripped = line.strip()
    if ':' in stripped and not stripped.startswith('/*') and not stripped.startswith('*') and not stripped.startswith('@') and not stripped.startswith('//'):
        brace_count = stripped.count('{') + stripped.count('}')
        if brace_count == 0 or (brace_count == 1 and ('{' in stripped or '}' in stripped)):
            parts = stripped.split(':')
            if len(parts) >= 2:
                prop = parts[0].strip()
                val_part = ':'.join(parts[1:]).rstrip(';').strip()
                if not val_part:
                    print(f'Line {i}: Property "{prop}" has no value')

for i, line in enumerate(lines, 1):
    if 'linear-gradient' in line:
        opens = line.count('(')
        closes = line.count(')')
        if opens != closes:
            print(f'Line {i}: Unbalanced parentheses in gradient')
            
print('Validation complete')
