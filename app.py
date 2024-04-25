from flask import Flask, request, render_template
from Summary_Question_Generator import QuestionGenerator

app = Flask(__name__)
generator = QuestionGenerator()




@app.route('/')
def home():
    return render_template('home.html')


@app.route('/summary', methods=['GET', 'POST'])
def summary():
    context = {'summary': '', 'text': ''}
    if request.method == 'POST':
        context['text'] = request.form.get('text', '').strip()
        if context['text']:
            context['summary'] = generator.generate_summary(context['text'])
    return render_template('summary.html', **context)

@app.route('/question', methods=['GET', 'POST'])
def question():
    context = {'qa_pairs': None, 'text': '', 'num_questions': 5}
    if request.method == 'POST':
        context['text'] = request.form.get('text', '').strip()
        num_questions = int(request.form.get('num_questions', 5))
        context['num_questions'] = num_questions
        if context['text']:
            context['qa_pairs'] = generator.generate(context['text'], use_evaluator=True, num_questions=num_questions,answer_style="all")
    return render_template('question.html', **context)

if __name__ == '__main__':
    app.run(debug=True, port=5001)
