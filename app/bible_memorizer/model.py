"""Bible Memorizer note type, copied 1:1 from src/model.py (version 1.5)."""

from textwrap import dedent

MODEL_NAME = 'Bible Memorizer'
MODEL_VERSION = '1.5'
FIELD_NAMES = ['answer', 'verse_number', 'verse_part', 'table', 'front', 'back', 'first_letters', 'last_letters', 'title', 'id']
SORT_FIELD = 'id'
CSS = dedent('\n    .card {\n        font-family: arial;\n        font-size: 20px;\n        text-align: center;\n        color: black;\n        background-color: white;\n    }\n\n    .back {\n        color: blue;\n        font-style: italic;\n    }\n\n    .table {\n        font-size: 16px;\n        margin: auto;\n        max-width: 600px;\n    }\n\n    table {\n        table-layout: fixed; \n        width: 100%; \n        border-collapse: collapse; \n        border: 1x solid;\n    }\n\n    td {\n        word-wrap: break-word; \n        vertical-align: top; \n        padding:.3em;\n    }\n\n    td:last-child {\n        background-color: orange;\n    }\n\n    .highlight {\n        background-color: yellow;\n    }\n\n    .card.nightMode {\n        background-color: #333;\n    }\n\n    .nightMode .back {\n        color: yellow;\n    }\n\n    .passage {\n        font-size: .8em\n    }\n\n    .front, .back {\n       font-family: serif\n    }\n').strip()

# (name, front, back) in the final order of version 1.5
TEMPLATES = [
    ('Card 1', '{{#last_letters}}\n<div id="card">\n<div>{{title}}</div>\n<div class="passage">{{verse_number}}{{verse_part}}</div>\n<br>\n<div class="front">{{front}}\n{{last_letters}}</div>\n</div>\n{{/last_letters}}', '{{FrontSide}}\n<br>\n<hr id="answer">\n<div class="back">{{answer}}</div>\n'),
    ('Card 2', '<div id="card">\n<div>{{title}}</div>\n<div class="passage">{{verse_number}}{{verse_part}}</div>\n<br>\n<div class="front">{{front}}\n{{first_letters}}</div>\n</div>', '{{FrontSide}}\n<br>\n<hr id="answer">\n<div class="back">{{answer}}</div>\n'),
    ('Card 3', '{{#table}}\n<div id="card">\n<div>{{title}}</div>\n<div class="passage">{{verse_number}}{{verse_part}}</div>\n<br>\n<div class="table">{{table}}</div>\n</div>\n{{/table}}', '{{FrontSide}}\n<br>\n<hr id="answer">\n<div class="back">{{answer}}</div>\n'),
    ('Card 4', '<div id="card">\n<div>{{title}}</div>\n<div class="passage">{{verse_number}}{{verse_part}}</div>\n<br>\n<div class="front">{{front}}</div>\n</div>', '{{FrontSide}}\n<br>\n<hr id="answer">\n<div class="back">{{back}}</div>'),
]
