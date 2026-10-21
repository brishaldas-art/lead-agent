from agent.fetch import fetch_page

result = fetch_page("https://www.python.org/about/")
print(result["status"], "|", result["final_url"], "|", len(result["text"]))