import glob
import sys

import yaml

bad = 0
for p in sorted(glob.glob("skills/*/SKILL.md")):
    text = open(p, encoding="utf-8").read()
    try:
        meta = yaml.safe_load(text.split("---")[1])
        desc, name = meta.get("description"), meta.get("name")
        problem = None
        if not isinstance(desc, str) or not 200 <= len(desc) <= 1024:
            problem = f"description must be a string of 200-1024 chars (got {type(desc).__name__}, {len(desc or '')})"
        elif name != p.replace("\\", "/").split("/")[1]:
            problem = f"name '{name}' doesn't match its folder"
    except yaml.YAMLError as e:
        problem = "frontmatter isn't valid YAML: " + str(e).splitlines()[0]
    print(("FAIL " if problem else "ok   ") + p + (f": {problem}" if problem else ""))
    bad += bool(problem)
sys.exit(1 if bad else 0)
