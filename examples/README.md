# Exemplo sanitizado

`wispr-transcript.txt` imita a estrutura timestampada usada no fluxo real sem reproduzir uma aula, material ou pessoa existente.

Para uma demonstração pública, grave um `.mov` curto mostrando código ou slides criados por você e alinhe o início da gravação ao timestamp correspondente:

```bash
uv run prepare_lecture.py demo.mov \
  -t examples/wispr-transcript.txt \
  --offsets 00:00:05 \
  -o lectures/demo \
  --title "Demonstração sanitizada"
```

O diretório `lectures/` continuará local e ignorado pelo Git. Revise todos os frames antes de compartilhar qualquer saída.
