def process_detail_page(detail_url: str, state: dict) -> None:
    soup = get_soup(detail_url)
    page_text = extract_page_text(soup)
    title = extract_title(soup)

    result_text = parse_result_sentence(page_text)
    schwinger = find_first_number_after("Anzahl Schwinger", page_text)
    zuschauer = find_first_number_after("Anzahl Zuschauer", page_text)

    if result_text and detail_url not in state["sent_results"]:
        lines = [f"🏆 {title}", "", f"🥇 {result_text}"]

        if schwinger:
            lines.append("")
            lines.append(f"👥 Schwinger: {schwinger}")
        if zuschauer:
            lines.append(f"👀 Zuschauer: {zuschauer}")

        send_message("\n".join(lines))
        state["sent_results"].append(detail_url)
        save_state(state)

    allowed_keywords = [
        "zwischenrangliste",
        "schlussrangliste",
        "statistik",
    ]

    for link in soup.find_all("a", href=True):
        href = link["href"]
        link_text = link.get_text(" ", strip=True)

        if ".pdf" not in href.lower():
            continue

        combined = f"{href} {link_text}".lower()

        if not any(keyword in combined for keyword in allowed_keywords):
            print(f"PDF ignoriert: {combined}")
            continue

        pdf_url = normalise_url(href)

        if pdf_url in state["sent_pdfs"]:
            continue

        pdf_type = detect_pdf_type(combined)

        caption_lines = [
            pdf_type,
            "",
            f"📍 {title}",
        ]

        if link_text:
            caption_lines.append(f"📝 {link_text}")

        send_document(pdf_url, "\n".join(caption_lines))
        state["sent_pdfs"].append(pdf_url)
        save_state(state)
