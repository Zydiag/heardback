Output public/docs/demo.gif

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

Type "jv list | head -n 8 | cut -c1-80"
Enter
Sleep 2s
Type "jv tmux list"
Enter
Sleep 2s
Type "jv tmux new --name webapp"
Enter
Sleep 2s
Type "jv tmux type 1 'echo hello from jv'"
Enter
Sleep 4s
