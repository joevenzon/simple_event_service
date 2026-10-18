### Comments and Readability
- **Explain what the thing does**: Start function and class comments with their purpose and behavior: what they do, what result they produce or output they modify, and why a caller would use them. Follow with input restrictions, error behavior, ownership, and other relevant details.
- **Separate logical steps**: Use blank lines to separate code into steps, including distinct stages within loops. Avoid dense runs of statements that perform different tasks.
- **Explain code blocks**: Add short comments before logical blocks to explain what each stage does. Use whitespace between these blocks so the code is easy to scan.
- **Timeless comments**: Ensure comments don't reference temporary implementation notes or recent changes. They always need to explain the full current state of the code.
- If there isn't at least one comment or line break after 5-10 lines, you're probably doing it wrong.
