# VHS tape for the Mnemo rollback demo GIF.
#   brew install vhs   (https://github.com/charmbracelet/vhs)
#   make gif           # -> docs/rollback.gif
#
# Prereqs: `make up && make migrate` already run, and Ollama serving
# nomic-embed-text + llama3.2:3b.

Output docs/rollback.gif

Set Shell "bash"
Set FontSize 15
Set Width 1180
Set Height 820
Set Padding 22
Set Theme "Dracula"
Set TypingSpeed 60ms

Hide
Type "clear"  Enter
Show

Type "make demo"
Sleep 700ms
Enter
Sleep 13s
