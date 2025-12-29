SteamOS/Steam Deck–like compatibility > minimal dependencies > simple modification to behavior.
Simplicity of code > readability of code > all else.

All dynamic/configurable values should be initialized to settings from a default value if they are not present, and read from settings when used. this supercedes the constants preference. This includes things like window/font size, paths, or anything that is likely to need some flexibility, but should also persist across runs
Prefer using shared constants to literals where reasonable which should be consolidated to constants.py where possible.
Prefer reductive changes that solve a problem where reasonable without affecting the default behavior.
Ask clarifying questions before beginning if instructions are unclear, conflicting, or seem to disagree with the goal.
Ask about any cleanup that might be possible for touched areas of code rather than just piling more code on top of it.

Do not worry about preserving legacy code or compatibility.
Offer unsolicited suggestions or alternatives where reasonable.
If the repo could benefit from a change to the agents.md file, please make suggestions.
Prefer modularity where it makes sense.

This repo is designed to facilitate the use of Archipelago with BizHawk on a Steam Deck–like device.
The goal is to remove as many user actions as is reasonable, but keep it functional, flexible, and safe.
The only supported platform is a Steam Deck–like running SteamOS.
