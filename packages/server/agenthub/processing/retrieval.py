"""Bounded lexical reranking with explicit version compatibility and abstention."""
import re


GENERIC={'fixture','version','public','configuration','config','setting','settings','successful','recover','recovery','repair',
    'does','did','agent','assistant','earlier','check','steps','task','use','uses','need','needs','needed','requires','please','how','what',
    'error','failed','failure',
    'for','should','can','could','would','will','must','may','might','are','was','were',
    'you','your','our','their','them','they','its','into','onto','under','over','also',
    'only','such','some','more','most','any','all','other','than','there','these','those',
    'had','has','been','being','not','and','but','apply','applies','using'}
ALIASES={'retries':'retry','retrying':'retry','migrating':'migration','migrate':'migration','backups':'backup','workers':'worker',
         'remaining':'remain','remains':'remain'}
TEMPORAL_WORDS={'latest','newest','current','currently','now','today','recent'}
MIN_QUERY_COVERAGE=0.34



# Only complete, narrowly recognized presentation/provenance directives are omitted
# from the search query. Unknown words, numbers, code and questions remain intact.
# This does not change the user's prompt, capture, or confidentiality checks.
RESPONSE_WORDS=set('''answer answers respond reply return include cite only from context
already delivered without tools tool searches search memory memories files file reads
read the a an reference references id ids if one was were is are has been otherwise
unknown no none applicable available evidence information say so do not use or and
your sources source citations citation concisely briefly in numbered bullet list
plain text markdown format json please with'''.split())


def subject_query(query):
    parts=re.split(r'(?<=[.!?])\s+|\n+',query)
    kept=[];removed=False
    for part in parts:
        words=re.findall(r'[A-Za-z]+',part.lower())
        presentation=(words and words[0] in {'answer','respond','reply','return','include','cite'}
                      and set(words)<=RESPONSE_WORDS
                      and re.fullmatch(r'[A-Za-z\s,;.!:-]+',part))
        if presentation:removed=True
        else:kept.append(part)
    return ' '.join(kept).strip() if removed else query
