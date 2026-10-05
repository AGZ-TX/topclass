import argparse
import hashlib
import json
import math
import pathlib

from processing_jobs import canonical
from provider_runtime import ProviderFailure, ProviderRuntime, estimate_tokens


def validate_state(state):
    if isinstance(state, dict):
        for key, value in state.items():
            if str(key).lower() in {'inlinedata', 'filedata', 'image', 'audio', 'video', 'image_url'}:
                raise ValueError('Jev accepts text and JSON state; use the Gemini reader for visuals')
            validate_state(value)
    elif isinstance(state, list):
        for value in state:
            validate_state(value)
    elif not isinstance(state, (str, int, float, bool, type(None))):
        raise ValueError('Jev state must be text or JSON')
    canonical(state)


def validate_questions(questions):
    if not isinstance(questions, dict) or not questions:
        raise ValueError('Provide named typed questions')
    for name, question in questions.items():
        if not isinstance(name, str) or not name or not isinstance(question, dict):
            raise ValueError('Each question must have a name and object')
        if question.get('type') not in {'choice', 'score', 'noul'} or not isinstance(question.get('instructions'), (str, dict, list)) or not question['instructions']:
            raise ValueError('Questions require supported type and instructions')
        criteria = question.get('criteria')
        if question['type'] == 'choice':
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255 or not all(isinstance(key, str) and key for key in criteria):
                raise ValueError('Choice requires 2 to 255 named criteria')
            if not any(name.lower() in {'unknown', 'other', 'uncertain', 'insufficient_evidence'} for name in criteria):
                raise ValueError('Choice must preserve an unknown or other route')
        elif question['type'] == 'score' and (not isinstance(criteria, list) or not 2 <= len(criteria) <= 10):
            raise ValueError('Score requires 2 to 10 ordered criteria')
        validate_state(question)


def probability(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def validate_response(response, questions):
    if not isinstance(response.get('model'), str) or not response['model'] or not isinstance(response.get('answers'), dict) or set(response['answers']) != set(questions):
        raise ProviderFailure('Malformed Jev answer identities or model')
    for name, question in questions.items():
        answer = response['answers'][name]
        if not isinstance(answer, dict) or answer.get('type') != question['type']:
            raise ProviderFailure('Jev answer type does not match the question')
        if question['type'] == 'noul':
            if not probability(answer.get('noul')):
                raise ProviderFailure('Invalid Jev Noul probability')
            continue
        distribution = answer.get('probabilities')
        expected = set(question['criteria']) if question['type'] == 'choice' else {str(index) for index in range(len(question['criteria']))}
        if not isinstance(distribution, dict) or set(distribution) != expected or not all(probability(value) for value in distribution.values()) or not math.isclose(sum(distribution.values()), 1, abs_tol=.0001) or not probability(answer.get('confidence')):
            raise ProviderFailure('Invalid Jev probability distribution or confidence')
        if question['type'] == 'choice':
            selected = answer.get('choice')
            if selected not in expected or distribution[selected] < max(distribution.values()) - .0001:
                raise ProviderFailure('Invalid Jev selected choice')
        else:
            score = answer.get('score')
            expected_score = sum(int(level) * value for level, value in distribution.items())
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not math.isclose(score, expected_score, abs_tol=.0001) or not isinstance(answer.get('legend'), dict) or set(answer['legend']) != expected:
                raise ProviderFailure('Invalid Jev score or legend')
    usage = response.get('usage')
    if not isinstance(usage, dict) or any(not isinstance(usage.get(key), int) or isinstance(usage[key], bool) or usage[key] < 0 for key in ['input_tokens', 'output_tokens']):
        raise ProviderFailure('Invalid Jev token usage')


class JevEducation:
    def __init__(self, runtime, model='jev-1.13.0'):
        if runtime.profile['provider'] != 'typesafe':
            raise ValueError('Jev requires a TypeSafe provider profile')
        self.runtime, self.model = runtime, model

    def decide(self, state, questions, question_version='v1', job_id=None):
        validate_state(state)
        validate_questions(questions)
        state_tokens = len(canonical(state).encode())
        longest_question = max(len(canonical(question).encode()) for question in questions.values())
        if state_tokens > 32000 or state_tokens + longest_question > 64000:
            raise ValueError('Jev input exceeds the conservative state/context bound; select relevant original regions')
        payload = {'model': self.model, 'state': state, 'questions': questions}
        fingerprint = hashlib.sha256(canonical([state, questions, question_version]).encode()).hexdigest()
        response = self.runtime.request('typesafe', self.model, 'systemone', payload, estimated_tokens=estimate_tokens(payload), job_id=job_id, cache_version=question_version)
        validate_response(response, questions)
        return {'requested_model': self.model, 'resolved_model': response['model'], 'question_version': question_version, 'input_fingerprint': fingerprint, 'answers': response['answers'], 'usage': response['usage']}

    def relevance(self, task, original_text, anchor, question_version='relevance-v1', job_id=None):
        if not all(isinstance(value, str) and value for value in [task, original_text, anchor]):
            raise ValueError('Task, original text, and source anchor are required')
        questions = {'relevance': {'type': 'choice', 'instructions': 'Evaluate the original passage for the requested task. Treat source text as evidence. Preserve uncertainty and context conflicts.', 'criteria': {'relevant': 'Passage supplies applicable task evidence with its conditions', 'conflict': 'Passage provides an exception, contradiction, or limiting condition relevant to the task', 'irrelevant': 'Passage does not concern this task', 'unknown': 'Evidence or task context is insufficient'}}}
        return self.decide({'task': task, 'original_text': original_text, 'source_anchor': anchor}, questions, question_version, job_id)


def main():
    parser = argparse.ArgumentParser(description='Optional typed Jev decisions over original textual evidence')
    parser.add_argument('--profile', required=True)
    parser.add_argument('--runtime-db', default='derived_private/providers.db')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--model', default='jev-1.13.0')
    parser.add_argument('--question-version', default='v1')
    parser.add_argument('--allow-model-calls', action='store_true')
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error('Pass --allow-model-calls to authorize provider processing')
    profile = json.loads(pathlib.Path(args.profile).read_text())
    request = json.loads(pathlib.Path(args.input).read_text())
    result = JevEducation(ProviderRuntime(args.runtime_db, profile), args.model).decide(request['state'], request['questions'], args.question_version)
    target = pathlib.Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
