.PHONY: test check

test:
	python3 -m unittest discover -v

check:
	python3 -m compileall -q macqueue tests
	python3 -m unittest discover -v

