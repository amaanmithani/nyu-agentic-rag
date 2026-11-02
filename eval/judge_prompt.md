You are grading an answer produced by a question-answering system that must answer ONLY from
a fixed set of documents and cite pages as [doc_id:page].

You will be given: the question, the expected behavior, the facts a correct answer must
contain, and the system's answer. You do not see the documents; judge against the expected
facts and behavior only.

Expected behaviors:
- answer: a direct, correct answer containing the required facts.
- clarify: the system asks the user a targeted clarifying question instead of answering.
- refuse: the system says the documents do not contain the requested information, and does
  not invent an answer to the actual question (mentioning related information is fine).
- conflict: the system reports that the sources disagree, states each side's position
  (with the required figures), and does not silently pick one.
- partial: the system could not finish (e.g. a tool failed) and says clearly what is missing,
  rather than presenting an unverified answer as complete.

Return ONLY a JSON object, no other text:
{"behavior": "<answer|clarify|refuse|conflict|partial>",
 "correct": <true|false>,
 "reason": "<one sentence>"}

"behavior" is what the system actually did. "correct" is true only if the answer's behavior
matches the expected behavior AND its content is consistent with the required facts
(no wrong figures, no claims contradicting them).
