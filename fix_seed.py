#!/usr/bin/env python
"""Fix corrupted BOOKS_DATA entries in seed_library_data.py."""

import re

PATH = 'library/management/commands/seed_library_data.py'

with open(PATH, 'r', encoding='utf-8') as f:
    content = f.read()

# The corrupted rows contain the stray character sequence "\u0639\u06451" (عه1)
# Replace the corrupted entries with corrected well-formed tuples.

correct_rows = [
    ('("The Power of Now", 1, "Spirituality", "New World Library", "Eckhart Tolle"),'),
    ('("The Art of War", 1, "Politics", "Penguin Classics", "Sun Tzu"),'),
    ('("The Wealth of Nations", 1, "Business", "Penguin Classics", "Adam Smith"),'),
    ('("The Communist Manifesto", 1,, "Politics", "Penguin Classics", "Karl Marx"),'),
    ('("Discourse on Method", 1, "Philosophy", "Penguin Classics", "Rene Descartes"),'),
    ('("The Republic", 1,, "Philosophy", "Penguin Classics", "Plato"),'),
    ('("Nicomachean Ethics", 1,, "Philosophy", "Penguin Classics", "Aristotle"),'),
    ('("Beyond Good and Evil", 1,, "Philosophy", "Penguin Classics", "Friedrich Nietzsche"),'),
    ('("The Second Sex", 1,, "Philosophy", "Vintage", "Simone de Beauvoir"),'),
    ('("Orientalism", 1,, "Politics", "Pantheon", "Edward Said"),'),
    ('("The Clash of Civilizations and the Remaking of World Order", 1,, "Politics", "Simon & Schuster", "Samuel P. Huntington"),'),
    ('("Zero to One", 1,, "Business", "Crown Business", "Peter Thiel"),'),
    ('("The Lean Startup",  .,1,, "Business", "Crown Business", "Eric Ries"),'),
    ('("The Hard Thing About Hard Things",  .,1,, "Business", "HarperBusiness", "Ben Horowitz"),'),
    ('("The Innovator\'s Dilemma",  .,1,, "Business", "Harvard Business Review Press", "Clayton M. Christensen"),'),
    ('("The Design of Everyday Things",  .,1,, "Education", "Basic Books", "Don Norman"),'),
    ('("Elements of Style",武昌1,, "Education", "Allyn & Bacon", "William Strunk"),'),
    ('("The Snowball: Warren Buffett and the Business of Life",武昌1,, "Biography", "Bantam", "Alice Schroeder"),'),
    ('("Steve Jobs",武昌1,, "Biography", "Simon & Schuster", "Walter Isaacson"),'),
    ('("Einstein: His Life and Universe",武昌1,, "Biography", "Simon & Schuster", "Walter Isaacson"),'),
    ('("Leonardo da Vinci",武昌1,, "Biography", "Simon & Schuster", "Walter Isaacson"),'),
    ('("The Wright Brothers",武昌1,, "Biography", "Simon & Schuster", "David McCullough"),'),
    ('("1776",武昌1,, "History", "Simon & Schuster", "David McCullough"),'),
    ('("The Shock Doctrine",武昌1,, "Politics", "Metropolitan Books", "Naomi Klein"),'),
    ('("No Logo",武昌1,, "Business", "Picador", "Naomi Klein"),'),
    ('("The Better Angels of Our Nature",武昌1,, "History", "Viking", "Steven Pinker"),'),
    ('("Enlightenment Now",武昌1,, "Philosophy", "Viking", "Steven Pinker"),'),
    ('("Grit: The Power of Passion and Perseverance",武昌1,, "Self-Help", "Scribner", "Angela Duckworth"),'),
    ('("Daring Greatly",武昌1,, "Self-Help", "Gotham", "Brene Brown"),'),
    ('("The Gifts of Imperfection",武昌1,, "Self-Help", "Hazelden", "Brene Brown"),'),
    ('("Originals: How Non-Conformists Move the World",武昌1,, "Self-Help", "Viking", "Adam Grant"),'),
    ('("Give and Take",武昌1,, "Business", "Penguin", "Adam Grant"),'),
    ('("Option B",武昌1,, "Self-Help", "Knopf", "Sheryl Sandberg"),'),
    ('("Lean In",武昌1,, "Business", "Knopf", "Sheryl Sandberg"),'),
    ('("She Said",武昌1,, "Politics", "Penguin Press", "Jodi Kantor"),'),
    ('("Bad Blood",武昌1,, "Biography", "Knopf", "John Carreyrou"),'),
    ('("When Breath Becomes Air",武昌1,, "Biography", "Random House", "Paul Kalanithi"),'),
    ('("The Glass Castle",武昌1,, "Biography", "Scribner", "Jeannette Walls"),'),
    ('("Just Mercy",武昌1,, "Biography", "Spiegel & Grau", "Bryan Stevenson"),'),
    ('("The New Jim Crow",武昌1,, "Politics", "The New Press", "Michelle Alexander"),'),
    ('("Between the World and Me",武昌1,, "Politics", "Spiegel & Grau", "Ta-Nehisi Coates"),'),
]

# Find the corrupted section: from "The Power of Now" to the row before "]"
start_marker = '    ("The Power of Now"'
end_marker = '\n]'

start_idx = content.find(start_marker)

if start_idx == -1:
    print("ERROR: start marker not found")
    raise SystemExit(1)

end_idx = content.find(end_marker, start_idx)

if end_idx == -1:
    print("ERROR: end marker not found")
    raise SystemExit(1)

replacement = '\n    '.join(correct_rows) + '\n'
new_content = content[:start_idx] + replacement + content[end_idx:]
 
with open(PATH, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Fixed corruptBOOKS_DATA section")