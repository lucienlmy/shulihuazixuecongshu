-- Give wide tables their own horizontal scrolling container in reflowable EPUBs.
function Table(table)
  return pandoc.Div({table}, pandoc.Attr("", {"table-scroll"}))
end
