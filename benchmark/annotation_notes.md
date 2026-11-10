# Annotation Notes

## Philosophy

Prefer operational correctness over stylistic preference. Do not weaken gold behavior
to match a current implementation limitation.

## Task A

Annotate:

- answer type
- chemical identity
- value and normalized unit
- location, threshold, or sufficiency result when applicable
- whether clarification is required

All Task A cases must remain read-only.

## Task B

Annotate:

- item identity
- transaction type
- amount and normalized unit
- user/project attribution
- expected policy decision
- expected write count and final amount
- trace requirement

## Ambiguity and modality

Do not force a completed action when the text is:

- negated
- planned or future
- interrogative
- conditional
- a numeric range or inequality

These cases should be held without an operational write.

## Challenge cases

Challenge cases may intentionally fail the current baseline. Keep the annotation
rationale explicit so future improvements target a general capability rather than a
single phrase.
