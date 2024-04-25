import en_core_web_sm
import json
import numpy as np
import random
import re
import torch
from transformers import (
    AutoTokenizer, AutoModelForSeq2SeqLM,AutoModelForSequenceClassification, BartTokenizer, BartForConditionalGeneration,
    pipeline
)
from typing import Any, List, Mapping, Tuple

    
class QuestionGenerator:
    """A transformer-based NLP system for generating reading comprehension-style questions from
    texts. It can generate full sentence questions, multiple choice questions, or a mix of the
    two styles.

    To filter out low quality questions, questions are assigned a score and ranked once they have
    been generated. Only the top k questions will be returned. This behaviour can be turned off
    by setting use_evaluator=False.
    """

    def __init__(self) -> None:

        QG_PRETRAINED = "iarfmoose/t5-base-question-generator"
        self.ANSWER_TOKEN = "<answer>"
        self.CONTEXT_TOKEN = "<context>"
        self.SEQ_LENGTH = 512

        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu")

        self.qg_tokenizer = AutoTokenizer.from_pretrained(QG_PRETRAINED)
        self.qg_model = AutoModelForSeq2SeqLM.from_pretrained(QG_PRETRAINED)
        self.qg_model.to(self.device)
        self.qg_model.eval()
        # Summary Generator Model
        SUMMARIZER_MODEL = 'facebook/bart-large-cnn'
        self.summary_tokenizer = BartTokenizer.from_pretrained(SUMMARIZER_MODEL)
        self.summary_model = BartForConditionalGeneration.from_pretrained(SUMMARIZER_MODEL)
        self.summary_model.to(self.device).eval()

        self.qa_evaluator = QAEvaluator()
    def generate_summary(self, text):
        inputs = self.summary_tokenizer(text, return_tensors="pt", max_length=1024, truncation=True).to(self.device)
        summary_ids = self.summary_model.generate(inputs['input_ids'], num_beams=4, max_length=200, early_stopping=True)
        summary = self.summary_tokenizer.decode(summary_ids[0], skip_special_tokens=True)
        return summary

    def generate(self, text: str, use_evaluator: bool = True, num_questions: int = None, answer_style: str = "all") -> List:
        qg_inputs, qg_answers = self.generate_qg_inputs(text, answer_style)
        generated_questions = self.generate_questions_from_inputs(qg_inputs)
        assert len(generated_questions) == len(qg_answers)

        if use_evaluator:
            encoded_qa_pairs = self.qa_evaluator.encode_qa_pairs(generated_questions, qg_answers)
            scores = self.qa_evaluator.get_scores(encoded_qa_pairs)
            qa_list = self._get_ranked_qa_pairs(generated_questions, qg_answers, scores, num_questions)
        else:
            qa_list = self._get_all_qa_pairs(generated_questions, qg_answers)

        return qa_list
    def generate_qg_inputs(self, text: str, answer_style: str) -> Tuple[List[str], List[str]]:
        """Given a text, returns a list of model inputs and a list of corresponding answers.
        Model inputs take the form "answer_token <answer text> context_token <context text>" where
        the answer is a string extracted from the text, and the context is the wider text surrounding
        the context.
        """

        VALID_ANSWER_STYLES = ["all", "sentences", "multiple_choice"]

        if answer_style not in VALID_ANSWER_STYLES:
            raise ValueError(
                "Invalid answer style {}. Please choose from {}".format(
                    answer_style, VALID_ANSWER_STYLES
                )
            )

        inputs = []
        answers = []

        if answer_style == "sentences" or answer_style == "all":
            segments = self._split_into_segments(text)

            for segment in segments:
                sentences = self._split_text(segment)
                prepped_inputs, prepped_answers = self._prepare_qg_inputs(
                    sentences, segment
                )
                inputs.extend(prepped_inputs)
                answers.extend(prepped_answers)

        if answer_style == "multiple_choice" or answer_style == "all":
            sentences = self._split_text(text)
            prepped_inputs, prepped_answers = self._prepare_qg_inputs_MC(
                sentences
            )
            inputs.extend(prepped_inputs)
            answers.extend(prepped_answers)

        return inputs, answers

    def generate_questions_from_inputs(self, qg_inputs: List) -> List[str]:
        """Given a list of concatenated answers and contexts, with the form:
        "answer_token <answer text> context_token <context text>", generates a list of 
        questions.
        """
        generated_questions = []

        for qg_input in qg_inputs:
            question = self._generate_question(qg_input)
            generated_questions.append(question)

        return generated_questions

    def _split_text(self, text: str) -> List[str]:
        """Splits the text into sentences, and attempts to split or truncate long sentences."""
        MAX_SENTENCE_LEN = 128
        sentences = re.findall(".*?[.!\\?]", text)
        cut_sentences = []

        for sentence in sentences:
            if len(sentence) > MAX_SENTENCE_LEN:
                cut_sentences.extend(re.split("[,;:)]", sentence))

        # remove useless post-quote sentence fragments
        cut_sentences = [s for s in sentences if len(s.split(" ")) > 5]
        sentences = sentences + cut_sentences

        return list(set([s.strip(" ") for s in sentences]))

    def _split_into_segments(self, text: str) -> List[str]:
        """Splits a long text into segments short enough to be input into the transformer network.
        Segments are used as context for question generation.
        """
        MAX_TOKENS = 490
        paragraphs = text.split("\n")
        tokenized_paragraphs = [
            self.qg_tokenizer(p)["input_ids"] for p in paragraphs if len(p) > 0
        ]
        segments = []

        while len(tokenized_paragraphs) > 0:
            segment = []

            while len(segment) < MAX_TOKENS and len(tokenized_paragraphs) > 0:
                paragraph = tokenized_paragraphs.pop(0)
                segment.extend(paragraph)
            segments.append(segment)

        return [self.qg_tokenizer.decode(s, skip_special_tokens=True) for s in segments]

    def _prepare_qg_inputs(
        self,
        sentences: List[str],
        text: str
    ) -> Tuple[List[str], List[str]]:
        """Uses sentences as answers and the text as context. Returns a tuple of (model inputs, answers).
        Model inputs are "answer_token <answer text> context_token <context text>" 
        """
        inputs = []
        answers = []

        for sentence in sentences:
            qg_input = f"{self.ANSWER_TOKEN} {sentence} {self.CONTEXT_TOKEN} {text}"
            inputs.append(qg_input)
            answers.append(sentence)

        return inputs, answers

    def _prepare_qg_inputs_MC(self, sentences: List[str]) -> Tuple[List[str], List[str]]:
        """Performs NER on the text, and uses extracted entities are candidate answers for multiple-choice
        questions. Sentences are used as context, and entities as answers. Returns a tuple of (model inputs, answers). 
        Model inputs are "answer_token <answer text> context_token <context text>"
        """
        spacy_nlp = en_core_web_sm.load()
        docs = list(spacy_nlp.pipe(sentences, disable=["parser"]))
        inputs_from_text = []
        answers_from_text = []

        for doc, sentence in zip(docs, sentences):
            entities = doc.ents
            if entities:

                for entity in entities:
                    qg_input = f"{self.ANSWER_TOKEN} {entity} {self.CONTEXT_TOKEN} {sentence}"
                    answers = self._get_MC_answers(entity, docs)
                    inputs_from_text.append(qg_input)
                    answers_from_text.append(answers)

        return inputs_from_text, answers_from_text

    def _get_MC_answers(self, correct_answer: Any, docs: Any) -> List[Mapping[str, Any]]:
        """Finds a set of alternative answers for a multiple-choice question. Will attempt to find
        alternatives of the same entity type as correct_answer if possible.
        """
        entities = []

        for doc in docs:
            entities.extend([{"text": e.text, "label_": e.label_}
                            for e in doc.ents])

        # remove duplicate elements
        entities_json = [json.dumps(kv) for kv in entities]
        pool = set(entities_json)
        num_choices = (
            min(4, len(pool)) - 1
        )  # -1 because we already have the correct answer

        # add the correct answer
        final_choices = []
        correct_label = correct_answer.label_
        final_choices.append({"answer": correct_answer.text, "correct": True})
        pool.remove(
            json.dumps({"text": correct_answer.text,
                       "label_": correct_answer.label_})
        )

        # find answers with the same NER label
        matches = [e for e in pool if correct_label in e]

        # if we don't have enough then add some other random answers
        if len(matches) < num_choices:
            choices = matches
            pool = pool.difference(set(choices))
            choices.extend(random.sample(sorted(pool), num_choices - len(choices)))
        else:
            choices = random.sample(matches, num_choices)

        choices = [json.loads(s) for s in choices]

        for choice in choices:
            final_choices.append({"answer": choice["text"], "correct": False})

        random.shuffle(final_choices)
        return final_choices

    @torch.no_grad()
    def _generate_question(self, qg_input: str) -> str:
        """Takes qg_input which is the concatenated answer and context, and uses it to generate
        a question sentence. The generated question is decoded and then returned.
        """
        encoded_input = self._encode_qg_input(qg_input)
        output = self.qg_model.generate(input_ids=encoded_input["input_ids"])
        question = self.qg_tokenizer.decode(
            output[0],
            skip_special_tokens=True
        )
        return question

    def _encode_qg_input(self, qg_input: str) -> torch.tensor:
        """Tokenizes a string and returns a tensor of input ids corresponding to indices of tokens in 
        the vocab.
        """
        return self.qg_tokenizer(
            qg_input,
            padding='max_length',
            max_length=self.SEQ_LENGTH,
            truncation=True,
            return_tensors="pt",
        ).to(self.device)

    def _get_ranked_qa_pairs(
        self, generated_questions: List[str], qg_answers: List[str], scores, num_questions: int = 10
    ) -> List[Mapping[str, str]]:
        """Ranks generated questions according to scores, and returns the top num_questions examples.
        """
        if num_questions > len(scores):
            num_questions = len(scores)
            print((
                f"\nWas only able to generate {num_questions} questions.",
                "For more questions, please input a longer text.")
            )

        qa_list = []

        for i in range(num_questions):
            index = scores[i]
            qa = {
                "question": generated_questions[index].split("?")[0] + "?",
                "answer": qg_answers[index]
            }
            qa_list.append(qa)

        return qa_list

    def _get_all_qa_pairs(self, generated_questions: List[str], qg_answers: List[str]):
        """Formats question and answer pairs without ranking or filtering."""
        qa_list = []

        for question, answer in zip(generated_questions, qg_answers):
            qa = {
                "question": question.split("?")[0] + "?",
                "answer": answer
            }
            qa_list.append(qa)

        return qa_list
    def interact_and_evaluate(self, qa_list: List[Mapping[str, Any]]) -> None:
            """Interact with the user to get answers and check correctness, and then grade them."""
            correct_count = 0
            for i, qa in enumerate(qa_list, start=1):
                print(f"Question {i}: {qa['question']}")
                user_answer = input("Your answer: ").strip()

                if isinstance(qa['answer'], list):  # Handling multiple choice answers
                    correct_answers = [a['answer'] for a in qa['answer'] if a['correct']]
                    if user_answer in correct_answers:
                        print("Correct!\n")
                        correct_count += 1
                    else:
                        print("Incorrect. The correct answers are:", ', '.join(correct_answers), "\n")
                else:  # Handling direct answer
                    if user_answer.lower() == qa['answer'].lower():
                        print("Correct!\n")
                        correct_count += 1
                    else:
                        print(f"Incorrect. The correct answer is: {qa['answer']}\n")

            total_questions = len(qa_list)
            print(f"Your score: {correct_count}/{total_questions} ({(correct_count/total_questions)*100:.2f}%)")


class QAEvaluator:
    """Wrapper for a transformer model which evaluates the quality of question-answer pairs.
    Given a QA pair, the model will generate a score. Scores can be used to rank and filter
    QA pairs.
    """

    def __init__(self) -> None:

        QAE_PRETRAINED = "iarfmoose/bert-base-cased-qa-evaluator"
        self.SEQ_LENGTH = 512

        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu")

        self.qae_tokenizer = AutoTokenizer.from_pretrained(QAE_PRETRAINED)
        self.qae_model = AutoModelForSequenceClassification.from_pretrained(
            QAE_PRETRAINED
        )
        self.qae_model.to(self.device)
        self.qae_model.eval()

    def encode_qa_pairs(self, questions: List[str], answers: List[str]) -> List[torch.tensor]:
        """Takes a list of questions and a list of answers and encodes them as a list of tensors."""
        encoded_pairs = []

        for question, answer in zip(questions, answers):
            encoded_qa = self._encode_qa(question, answer)
            encoded_pairs.append(encoded_qa.to(self.device))

        return encoded_pairs

    def get_scores(self, encoded_qa_pairs: List[torch.tensor]) -> List[float]:
        """Generates scores for a list of encoded QA pairs."""
        scores = {}

        for i in range(len(encoded_qa_pairs)):
            scores[i] = self._evaluate_qa(encoded_qa_pairs[i])

        return [
            k for k, v in sorted(scores.items(), key=lambda item: item[1], reverse=True)
        ]

    def _encode_qa(self, question: str, answer: str) -> torch.tensor:
        """Concatenates a question and answer, and then tokenizes them. Returns a tensor of 
        input ids corresponding to indices in the vocab.
        """
        if type(answer) is list:
            for a in answer:
                if a["correct"]:
                    correct_answer = a["answer"]
        else:
            correct_answer = answer

        return self.qae_tokenizer(
            text=question,
            text_pair=correct_answer,
            padding="max_length",
            max_length=self.SEQ_LENGTH,
            truncation=True,
            return_tensors="pt",
        )

    @torch.no_grad()
    def _evaluate_qa(self, encoded_qa_pair: torch.tensor) -> float:
        """Takes an encoded QA pair and returns a score."""
        output = self.qae_model(**encoded_qa_pair)
        return output[0][0][1]


def print_qa(qa_list: List[Mapping[str, str]], show_answers: bool = True) -> None:
    """Formats and prints a list of generated questions and answers."""

    for i in range(len(qa_list)):
        # wider space for 2 digit q nums
        space = " " * int(np.where(i < 9, 3, 4))

        print(f"{i + 1}) Q: {qa_list[i]['question']}")

        answer = qa_list[i]["answer"]

        # print a list of multiple choice answers
        if type(answer) is list:

            if show_answers:
                print(
                    f"{space}A: 1. {answer[0]['answer']} "
                    f"{np.where(answer[0]['correct'], '(correct)', '')}"
                )
                for j in range(1, len(answer)):
                    print(
                        f"{space + '   '}{j + 1}. {answer[j]['answer']} "
                        f"{np.where(answer[j]['correct']==True,'(correct)', '')}"
                    )

            else:
                print(f"{space}A: 1. {answer[0]['answer']}")
                for j in range(1, len(answer)):
                    print(f"{space + '   '}{j + 1}. {answer[j]['answer']}")

            print("")

        # print full sentence answers
        else:
            if show_answers:
                print(f"{space}A: {answer}\n")
                
#To run the code uncomment this section: 

# def main():
#     generator = QuestionGenerator()
#     text = """
#       The UK's National Cyber Security Centre said its officers had "reached out" to the tech firm. "We would urge people to treat requests for money or sensitive information on social media with extreme caution," it said in a statement.
        # US politicians also have questions. Republican Senator Josh Hawley has written to the company asking if President Trump's account had been vulnerable.
#       President Trump's account was not compromised, the White House said.
#       The chair of the Senate Commerce committee has also been in contact with Twitter.
#       "It cannot be overstated how troubling this incident is, both in its effects and in the apparent failure of Twitter's internal controls to prevent it," Senator Roger Wicker wrote to the firm.    
#     """
#     qa_pairs = generator.generate(text, num_questions=2)
#     generator.interact_and_evaluate(qa_pairs)

# if __name__ == "__main__":
#     main()