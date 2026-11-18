Output public/docs/readback.gif

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

Sleep 3s
Type "jv tmux close 1"
Enter
Sleep 3s
Type "jv tmux list"
Enter
Sleep 2s
Type "jv tmux close 1 --yes"
Enter
Sleep 6s
