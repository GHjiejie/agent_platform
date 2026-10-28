"""Deterministic browser code used only by isolated, zero-provider-I/O tests."""

COUNTER_UI = {
    "initialHeight": 180,
    "placeholderMessages": [],
    "css": "body{color:#433869}.counter{padding:20px}button{padding:10px}",
    "html": '<section class="counter"><h2>交互计数器</h2><output id="count">0</output><button id="add">增加</button></section>',
    "jsFunctions": "",
    "jsExpressions": [
        "let count=0; document.querySelector('#add').addEventListener('click',()=>{document.querySelector('#count').textContent=String(++count)});"
    ],
}
