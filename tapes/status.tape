Output public/docs/status.gif

Set Shell "bash"
Set FontSize 18
Set Width 1100
Set Height 620
Set TypingSpeed 35ms

# Isolation comes from render.sh; this only sets a plain prompt.
Hide
Type "PS1='$ ' PROMPT_COMMAND= && clear"
Enter
Show

Type "jv status list"
Enter
Sleep 15s
