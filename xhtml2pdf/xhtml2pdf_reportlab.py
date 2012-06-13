# -*- coding: utf-8 -*-

# Copyright 2010 Dirk Holtwick, holtwick.it
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from hashlib import md5
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import flatten, open_for_read, getStringIO, \
    LazyImageReader, haveImages
from reportlab.platypus.doctemplate import BaseDocTemplate, PageTemplate, IndexingFlowable
from reportlab.platypus.flowables import Flowable, CondPageBreak, \
    KeepInFrame, ParagraphAndImage
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.platypus.tables import Table, TableStyle
from xhtml2pdf.reportlab_paragraph import Paragraph
from xhtml2pdf.util import getUID, getBorderStyle
from types import StringType, TupleType, ListType, IntType
import StringIO
import cgi
import copy
import logging
import reportlab.pdfbase.pdfform as pdfform
import sys

try:
    import PIL.Image as PILImage
except:
    try:
        import Image as PILImage
    except:
        PILImage = None

log = logging.getLogger("xhtml2pdf")

MAX_IMAGE_RATIO = 0.95


class PTCycle(list):
    def __init__(self):
        self._restart = 0
        self._idx = 0
        list.__init__(self)

    def cyclicIterator(self):
        while 1:
            yield self[self._idx]
            self._idx += 1
            if self._idx >= len(self):
                self._idx = self._restart


class PmlMaxHeightMixIn:

    def setMaxHeight(self, availHeight):
        self.availHeightValue = availHeight
        if availHeight < 70000:
            if hasattr(self, "canv"):
                if not hasattr(self.canv, "maxAvailHeightValue"):
                    self.canv.maxAvailHeightValue = 0
                self.availHeightValue = self.canv.maxAvailHeightValue = max(
                    availHeight,
                    self.canv.maxAvailHeightValue)
        else:
            self.availHeightValue = availHeight
        if not hasattr(self, "availHeightValue"):
            self.availHeightValue = 0
        return self.availHeightValue

    def getMaxHeight(self):
        if not hasattr(self, "availHeightValue"):
            return 0
        return self.availHeightValue


class PmlBaseDoc(BaseDocTemplate):

    """
    We use our own document template to get access to the canvas
    and set some informations once.
    """

    def beforePage(self):

        # Tricky way to set producer, because of not real privateness in Python
        info = "pisa HTML to PDF <http://www.htmltopdf.org>"
        self.canv._doc.info.producer = info

        '''
        # Convert to ASCII because there is a Bug in Reportlab not
        # supporting other than ASCII. Send to list on 23.1.2007

        author = toString(self.pml_data.get("author", "")).encode("ascii","ignore")
        subject = toString(self.pml_data.get("subject", "")).encode("ascii","ignore")
        title = toString(self.pml_data.get("title", "")).encode("ascii","ignore")
        # print repr((author,title,subject))

        self.canv.setAuthor(author)
        self.canv.setSubject(subject)
        self.canv.setTitle(title)

        if self.pml_data.get("fullscreen", 0):
            self.canv.showFullScreen0()

        if self.pml_data.get("showoutline", 0):
            self.canv.showOutline()

        if self.pml_data.get("duration", None) is not None:
            self.canv.setPageDuration(self.pml_data["duration"])
        '''

    def afterFlowable(self, flowable):
        # Does the flowable contain fragments?
        if getattr(flowable, "outline", False):
            self.notify('TOCEntry', (
                flowable.outlineLevel,
                cgi.escape(copy.deepcopy(flowable.text), 1),
                self.page))

    def handle_nextPageTemplate(self, pt):
        '''
        if pt has also templates for even and odd page convert it to list
        '''
        has_left_template = self._has_template_for_name(pt + '_left')
        has_right_template = self._has_template_for_name(pt + '_right')

        if has_left_template and has_right_template:
            pt = [pt + '_left', pt + '_right']

        '''On endPage change to the page template with name or index pt'''
        if type(pt) is StringType:
            if hasattr(self, '_nextPageTemplateCycle'):
                del self._nextPageTemplateCycle
            for t in self.pageTemplates:
                if t.id == pt:
                    self._nextPageTemplateIndex = self.pageTemplates.index(t)
                    return
            raise ValueError("can't find template('%s')" % pt)
        elif type(pt) is IntType:
            if hasattr(self, '_nextPageTemplateCycle'):
                del self._nextPageTemplateCycle
            self._nextPageTemplateIndex = pt
        elif type(pt) in (ListType, TupleType):
            #used for alternating left/right pages
            #collect the refs to the template objects, complain if any are bad
            c = PTCycle()
            for ptn in pt:
                 #special case name used to short circuit the iteration
                if ptn == '*':
                    c._restart = len(c)
                    continue
                for t in self.pageTemplates:
                    if t.id == ptn.strip():
                        c.append(t)
                        break
            if not c:
                raise ValueError("No valid page templates in cycle")
            elif c._restart > len(c):
                raise ValueError("Invalid cycle restart position")

            #ensure we start on the first one$
            self._nextPageTemplateCycle = c.cyclicIterator()
        else:
            raise TypeError("Argument pt should be string or integer or list")

    def _has_template_for_name(self, name):
        for template in self.pageTemplates:
            if template.id == name.strip():
                return True
        return False


class PmlPageTemplate(PageTemplate):

    PORTRAIT = 'portrait'
    LANDSCAPE = 'landscape'
    # by default portrait
    pageorientation = PORTRAIT

    def __init__(self, **kw):
        self.pisaStaticList = []
        self.pisaBackgroundList = []
        self.pisaBackground = None
        PageTemplate.__init__(self, **kw)
        self._page_count = 0
        self._first_flow = True

    def isFirstFlow(self, canvas):
        if self._first_flow:
            if canvas.getPageNumber() <= self._page_count:
                self._first_flow = False
            else:
                self._page_count = canvas.getPageNumber()
        return self._first_flow

    def isPortrait(self):
        return self.pageorientation == self.PORTRAIT

    def isLandscape(self):
        return self.pageorientation == self.LANDSCAPE


    def beforeDrawPage(self, canvas, doc):
        canvas.saveState()
        try:

            # Background
            pisaBackground = None
            if (self.isFirstFlow(canvas)
                and hasattr(self, "pisaBackground")
                and self.pisaBackground
                and (not self.pisaBackground.notFound())):
                # print self.pisaBackground.mimetype

                # Is image not PDF
                if self.pisaBackground.mimetype.startswith("image/"):

                    try:
                        img = PmlImageReader(StringIO.StringIO(self.pisaBackground.getData()))
                        iw, ih = img.getSize()
                        pw, ph = canvas._pagesize

                        width = pw  # min(iw, pw) # max
                        wfactor = float(width) / iw
                        height = ph  # min(ih, ph) # max
                        hfactor = float(height) / ih
                        factor_min = min(wfactor, hfactor)

                        if self.isPortrait():
                            w = iw * factor_min
                            h = ih * factor_min
                            canvas.drawImage(img, 0, ph - h, w, h)
                        elif self.isLandscape():
                            factor_max = max(wfactor, hfactor)
                            w = ih * factor_max
                            h = iw * factor_min
                            canvas.drawImage(img, 0, 0, w, h)
                    except:
                        log.exception("Draw background")

                # PDF!
                else:
                    pisaBackground = self.pisaBackground

            # print "+", pisaBackground
            if pisaBackground:
                self.pisaBackgroundList.append(pisaBackground)

            # canvas.saveState()
            #try:
            #    self.pml_drawing.draw(canvas)
            #except Exception, e:
            #    # print "drawing exception", str(e)
            #    pass

            def pageNumbering(objList):
                for obj in flatten(objList):
                    if isinstance(obj, PmlParagraph):
                        for frag in obj.frags:
                            if frag.pageNumber:
                                frag.text = str(pagenumber)
                            elif frag.pageCount:
                                frag.text = str(self._page_count)

                    elif isinstance(obj, PmlTable):
                        # Flatten the cells ([[1,2], [3,4]] becomes [1,2,3,4])
                        flat_cells = [item for sublist in obj._cellvalues for item in sublist]
                        pageNumbering(flat_cells)
            try:

                # Paint static frames
                pagenumber = canvas.getPageNumber()
                for frame in self.pisaStaticList:

                    frame = copy.deepcopy(frame)
                    story = frame.pisaStaticStory
                    pageNumbering(story)

                    frame.addFromList(story, canvas)

            except Exception:  # TODO: Kill this!
                log.debug("PmlPageTemplate", exc_info=1)
        finally:
            canvas.restoreState()

_ctr = 1


class PmlImageReader(object):  # TODO We need a factory here, returning either a class for java or a class for PIL
    "Wraps up either PIL or Java to get data from bitmaps"
    _cache = {}

    def __init__(self, fileName):
        if isinstance(fileName, PmlImageReader):
            self.__dict__ = fileName.__dict__   # borgize
            return
        #start wih lots of null private fields, to be populated by
        #the relevant engine.
        self.fileName = fileName
        self._image = None
        self._width = None
        self._height = None
        self._transparent = None
        self._data = None
        imageReaderFlags = 0
        if PILImage and isinstance(fileName, PILImage.Image):
            self._image = fileName
            self.fp = getattr(fileName, 'fp', None)
            try:
                self.fileName = self._image.fileName
            except AttributeError:
                self.fileName = 'PILIMAGE_%d' % id(self)
        else:
            try:
                self.fp = open_for_read(fileName, 'b')
                if isinstance(self.fp, StringIO.StringIO().__class__):
                    imageReaderFlags = 0  # avoid messing with already internal files
                if imageReaderFlags > 0:  # interning
                    data = self.fp.read()
                    if imageReaderFlags & 2:  # autoclose
                        try:
                            self.fp.close()
                        except:
                            pass
                    if imageReaderFlags & 4:  # cache the data
                        if not self._cache:
                            from rl_config import register_reset
                            register_reset(self._cache.clear)
                        data = self._cache.setdefault(md5(data).digest(), data)
                    self.fp = getStringIO(data)
                elif imageReaderFlags == - 1 and isinstance(fileName, (str, unicode)):
                    #try Ralf Schmitt's re-opening technique of avoiding too many open files
                    self.fp.close()
                    del self.fp  # will become a property in the next statement
                    self.__class__ = LazyImageReader
                if haveImages:
                    #detect which library we are using and open the image
                    if not self._image:
                        self._image = self._read_image(self.fp)
                    if getattr(self._image, 'format', None) == 'JPEG':
                        self.jpeg_fh = self._jpeg_fh
                else:
                    from reportlab.pdfbase.pdfutils import readJPEGInfo
                    try:
                        self._width, self._height, c = readJPEGInfo(self.fp)
                    except:
                        raise RuntimeError('Imaging Library not available, unable to import bitmaps only jpegs')
                    self.jpeg_fh = self._jpeg_fh
                    self._data = self.fp.read()
                    self._dataA = None
                    self.fp.seek(0)
            except:  # TODO: Kill the catch-all
                et, ev, tb = sys.exc_info()
                if hasattr(ev, 'args'):
                    a = str(ev.args[- 1]) + (' fileName=%r' % fileName)
                    ev.args = ev.args[: - 1] + (a,)
                    raise et, ev, tb
                else:
                    raise

    def _read_image(self, fp):
        if sys.platform[0:4] == 'java':
            from javax.imageio import ImageIO
            return ImageIO.read(fp)
        elif PILImage:
            return PILImage.open(fp)

    def _jpeg_fh(self):
        fp = self.fp
        fp.seek(0)
        return fp

    def jpeg_fh(self):
        return None

    def getSize(self):
        if (self._width is None or self._height is None):
            if sys.platform[0:4] == 'java':
                self._width = self._image.getWidth()
                self._height = self._image.getHeight()
            else:
                self._width, self._height = self._image.size
        return (self._width, self._height)

    def getRGBData(self):
        "Return byte array of RGB data as string"
        if self._data is None:
            self._dataA = None
            if sys.platform[0:4] == 'java':
                import jarray  # TODO: Move to top.
                from java.awt.image import PixelGrabber
                width, height = self.getSize()
                buffer = jarray.zeros(width * height, 'i')
                pg = PixelGrabber(self._image, 0, 0, width, height, buffer, 0, width)
                pg.grabPixels()
                # there must be a way to do this with a cast not a byte-level loop,
                # I just haven't found it yet...
                pixels = []
                a = pixels.append
                for rgb in buffer:
                    a(chr((rgb >> 16) & 0xff))
                    a(chr((rgb >> 8) & 0xff))
                    a(chr(rgb & 0xff))
                self._data = ''.join(pixels)
                self.mode = 'RGB'
            else:
                im = self._image
                mode = self.mode = im.mode
                if mode == 'RGBA':
                    im.load()
                    self._dataA = PmlImageReader(im.split()[3])
                    im = im.convert('RGB')
                    self.mode = 'RGB'
                elif mode not in ('L', 'RGB', 'CMYK'):
                    im = im.convert('RGB')
                    self.mode = 'RGB'
                self._data = im.tostring()
        return self._data

    def getImageData(self):
        width, height = self.getSize()
        return width, height, self.getRGBData()

    def getTransparent(self):
        if sys.platform[0:4] == 'java':
            return None
        elif "transparency" in self._image.info:
            transparency = self._image.info["transparency"] * 3
            palette = self._image.palette
            try:
                palette = palette.palette
            except:
                palette = palette.data
            return map(ord, palette[transparency:transparency + 3])
        else:
            return None

    def __str__(self):
        try:
            return "PmlImageObject_%s" % hash(self.fileName.read())
        except:
            return self.fileName


class PmlImage(Flowable, PmlMaxHeightMixIn):

    #_fixedWidth = 1
    #_fixedHeight = 1

    def __init__(self, data, width=None, height=None, mask="auto", mimetype=None, **kw):
        self.kw = kw
        self.hAlign = 'CENTER'
        self._mask = mask
        self._imgdata = data
        # print "###", repr(data)
        self.mimetype = mimetype
        img = self.getImage()
        if img:
            self.imageWidth, self.imageHeight = img.getSize()
        self.drawWidth = width or self.imageWidth
        self.drawHeight = height or self.imageHeight

    def wrap(self, availWidth, availHeight):
        " This can be called more than once! Do not overwrite important data like drawWidth "
        availHeight = self.setMaxHeight(availHeight)
        # print "image wrap", id(self), availWidth, availHeight, self.drawWidth, self.drawHeight
        width = min(self.drawWidth, availWidth)
        wfactor = float(width) / self.drawWidth
        height = min(self.drawHeight, availHeight * MAX_IMAGE_RATIO)
        hfactor = float(height) / self.drawHeight
        factor = min(wfactor, hfactor)
        self.dWidth = self.drawWidth * factor
        self.dHeight = self.drawHeight * factor
        # print "imgage result", factor, self.dWidth, self.dHeight
        return (self.dWidth, self.dHeight)

    def getImage(self):
        #if self.kw:
        #    print "img", self.kw, hash(self._imgdata)
        img = PmlImageReader(StringIO.StringIO(self._imgdata))
        # print id(self._imgdata), hash(img.getRGBData())
        return img

    def draw(self):
        img = self.getImage()
        self.canv.drawImage(
            img,
            0, 0,
            self.dWidth,
            self.dHeight,
            mask=self._mask)

    def identity(self, maxLen=None):
        r = Flowable.identity(self, maxLen)
        return r


class PmlParagraphAndImage(ParagraphAndImage, PmlMaxHeightMixIn):

    def wrap(self, availWidth, availHeight):
        # print "# wrap", id(self), self.canv
        # availHeight = self.setMaxHeight(availHeight)
        self.I.canv = self.canv
        result = ParagraphAndImage.wrap(self, availWidth, availHeight)
        del self.I.canv
        return result

    def split(self, availWidth, availHeight):
        # print "# split", id(self)
        if not hasattr(self, "wI"):
            self.wI, self.hI = self.I.wrap(availWidth, availHeight)  # drawWidth, self.I.drawHeight
        return ParagraphAndImage.split(self, availWidth, availHeight)

# if 1:
#    import reportlab.platypus.paragraph
#    Paragraph = reportlab.platypus.paragraph.Paragraph
#    class PmlParagraph(reportlab.platypus.paragraph.Paragraph):
#        pass


class PmlParagraph(Paragraph, PmlMaxHeightMixIn):

    def _calcImageMaxSizes(self, availWidth, availHeight):
        self.hasImages = False
        availHeight = self.getMaxHeight()
        for frag in self.frags:
            if hasattr(frag, "cbDefn") and frag.cbDefn.kind == "img":
                self.hasImages = True
                img = frag.cbDefn
                # print "before", img.width, img.height
                width = min(img.width, availWidth)
                wfactor = float(width) / img.width
                height = min(img.height, availHeight * MAX_IMAGE_RATIO)  # XXX 99% because 100% do not work...
                hfactor = float(height) / img.height
                factor = min(wfactor, hfactor)
                img.height = img.height * factor
                img.width = img.width * factor
                # print "after", img.width, img.height

    def wrap(self, availWidth, availHeight):

        availHeight = self.setMaxHeight(availHeight)

        style = self.style

        self.deltaWidth = style.paddingLeft + style.paddingRight + style.borderLeftWidth + style.borderRightWidth
        self.deltaHeight = style.paddingTop + style.paddingBottom + style.borderTopWidth + style.borderBottomWidth

        # reduce the available width & height by the padding so the wrapping
        # will use the correct size
        availWidth -= self.deltaWidth
        availHeight -= self.deltaHeight

        # Modify maxium image sizes
        self._calcImageMaxSizes(availWidth, self.getMaxHeight() - self.deltaHeight)

        # call the base class to do wrapping and calculate the size
        Paragraph.wrap(self, availWidth, availHeight)

        #self.height = max(1, self.height)
        #self.width = max(1, self.width)

        # increase the calculated size by the padding
        self.width = self.width + self.deltaWidth
        self.height = self.height + self.deltaHeight

        return (self.width, self.height)

    def split(self, availWidth, availHeight):

        if len(self.frags) <= 0:
            return []

        #the split information is all inside self.blPara
        # if not hasattr(self,'blPara'):
        if not hasattr(self, 'deltaWidth'):
            self.wrap(availWidth, availHeight)

        availWidth -= self.deltaWidth
        availHeight -= self.deltaHeight

        #if self.hasImages:
        #    return []

        return Paragraph.split(self, availWidth, availHeight)

    def draw(self):

        # Insert page number
        '''
        if 0: #for line in self.blPara.lines:
            try:
                for frag in line.words:
                    #print 111,frag.pageNumber, frag.text
                    if frag.pageNumber:
                        frag.text = str(self.canv.getPageNumber())
            except Exception, e:
                log.debug("PmlParagraph", exc_info=1)
        '''

        # Create outline
        if getattr(self, "outline", False):

            # Check level and add all levels
            last = getattr(self.canv, "outlineLast", - 1) + 1
            while last < self.outlineLevel:
                # print "(OUTLINE",  last, self.text
                key = getUID()
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(
                    self.text,
                    key,
                    last,
                    not self.outlineOpen)
                last += 1
            self.canv.outlineLast = self.outlineLevel

            key = getUID()
            # print " OUTLINE", self.outlineLevel, self.text
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(
                self.text,
                key,
                self.outlineLevel,
                not self.outlineOpen)
            last += 1

        #else:
        #    print repr(self.text)[:80]

        # Draw the background and borders here before passing control on to
        # ReportLab. This is because ReportLab can't handle the individual
        # components of the border independently. This will also let us
        # support more border styles eventually.
        canvas = self.canv
        style = self.style
        bg = style.backColor
        leftIndent = style.leftIndent
        bp = 0  # style.borderPadding

        x = leftIndent - bp
        y = - bp
        w = self.width - (leftIndent + style.rightIndent) + 2 * bp
        h = self.height + 2 * bp

        if bg:
            # draw a filled rectangle (with no stroke) using bg color
            canvas.saveState()
            canvas.setFillColor(bg)
            canvas.rect(x, y, w, h, fill=1, stroke=0)
            canvas.restoreState()

        # we need to hide the bg color (if any) so Paragraph won't try to draw it again
        style.backColor = None

        # offset the origin to compensate for the padding
        canvas.saveState()
        canvas.translate(
            (style.paddingLeft + style.borderLeftWidth),
            -1 * (style.paddingTop + style.borderTopWidth))  # + (style.leading / 4)))

        # Call the base class draw method to finish up
        Paragraph.draw(self)
        canvas.restoreState()

        # Reset color because we need it again if we run 2-PASS like we
        # do when using TOC
        style.backColor = bg

        canvas.saveState()

        def _drawBorderLine(bstyle, width, color, x1, y1, x2, y2):
            # We need width and border style to be able to draw a border
            if width and getBorderStyle(bstyle):
                # If no color for border is given, the text color is used (like defined by W3C)
                if color is None:
                    color = style.textColor
                # print "Border", bstyle, width, color
                if color is not None:
                    canvas.setStrokeColor(color)
                    canvas.setLineWidth(width)
                    canvas.line(x1, y1, x2, y2)

        _drawBorderLine(style.borderLeftStyle,
                        style.borderLeftWidth,
                        style.borderLeftColor,
                        x, y, x, y + h)
        _drawBorderLine(style.borderRightStyle,
                        style.borderRightWidth,
                        style.borderRightColor,
                        x + w, y, x + w, y + h)
        _drawBorderLine(style.borderTopStyle,
                        style.borderTopWidth,
                        style.borderTopColor,
                        x, y + h, x + w, y + h)
        _drawBorderLine(style.borderBottomStyle,
                        style.borderBottomWidth,
                        style.borderBottomColor,
                        x, y, x + w, y)

        canvas.restoreState()


class PmlKeepInFrame(KeepInFrame, PmlMaxHeightMixIn):

    def wrap(self, availWidth, availHeight):
        availWidth = max(availWidth, 1.0)
        self.maxWidth = availWidth
        self.maxHeight = self.setMaxHeight(availHeight)
        return KeepInFrame.wrap(self, availWidth, availHeight)


class PmlTable(Table, PmlMaxHeightMixIn):

    def _normWidth(self, w, maxw):
        " Helper for calculating percentages "
        if type(w) == type(""):
            w = ((maxw / 100.0) * float(w[: - 1]))
        elif (w is None) or (w == "*"):
            w = maxw
        return min(w, maxw)

    def _listCellGeom(self, V, w, s, W=None, H=None, aH=72000):
        # print "#", self.availHeightValue
        if aH == 72000:
            aH = self.getMaxHeight() or aH
        return Table._listCellGeom(self, V, w, s, W=W, H=H, aH=aH)

    def wrap(self, availWidth, availHeight):

        self.setMaxHeight(availHeight)

        # Strange bug, sometime the totalWidth is not set !?
        try:
            self.totalWidth
        except:
            self.totalWidth = availWidth

        # Prepare values
        totalWidth = self._normWidth(self.totalWidth, availWidth)
        remainingWidth = totalWidth
        remainingCols = 0
        newColWidths = self._colWidths

        #print
        #print "TABLE", newColWidths

        # Calculate widths that are fix
        # IMPORTANT!!! We can not substitute the private value
        # self._colWidths therefore we have to modify list in place
        for i, colWidth in enumerate(newColWidths):
            if (colWidth is not None) or (colWidth == '*'):
                colWidth = self._normWidth(colWidth, totalWidth)
                remainingWidth -= colWidth
            else:
                remainingCols += 1
                colWidth = None
            newColWidths[i] = colWidth

        # Distribute remaining space
        minCellWidth = totalWidth * 0.01
        if remainingCols > 0:
            for i, colWidth in enumerate(newColWidths):
                if colWidth is None:
                    # print "*** ", i, newColWidths[i], remainingWidth, remainingCols
                    newColWidths[i] = max(minCellWidth, remainingWidth / remainingCols)  # - 0.1

        # Bigger than totalWidth? Lets reduce the fix entries propotionally

        # print "New values:", totalWidth, newColWidths, sum(newColWidths)

        # Call original method "wrap()"
        # self._colWidths = newColWidths

        if sum(newColWidths) > totalWidth:
            quotient = totalWidth / sum(newColWidths)
            # print quotient
            for i in range(len(newColWidths)):
                newColWidths[i] = newColWidths[i] * quotient

        # To avoid rounding errors adjust one col with the difference
        diff = sum(newColWidths) - totalWidth
        if diff > 0:
            newColWidths[0] -= diff

        # print "New values:", totalWidth, newColWidths, sum(newColWidths)

        return Table.wrap(self, availWidth, availHeight)


class PmlPageCount(IndexingFlowable):

    def __init__(self):
        IndexingFlowable.__init__(self)
        self.second_round = False

    def isSatisfied(self):
        s = self.second_round
        self.second_round = True
        return s

    def drawOn(self, canvas, x, y, _sW=0):
        pass

class PmlTableOfContents(TableOfContents):

    def wrap(self, availWidth, availHeight):
        "All table properties should be known by now."

        widths = (availWidth - self.rightColumnWidth,
                  self.rightColumnWidth)

        # makes an internal table which does all the work.
        # we draw the LAST RUN's entries!  If there are
        # none, we make some dummy data to keep the table
        # from complaining
        if len(self._lastEntries) == 0:
            _tempEntries = [(0, 'Placeholder for table of contents', 0)]
        else:
            _tempEntries = self._lastEntries

        lastMargin = 0
        tableData = []
        tableStyle = [
            ('VALIGN', (0, 0), (- 1, - 1), 'TOP'),
            ('LEFTPADDING', (0, 0), (- 1, - 1), 0),
            ('RIGHTPADDING', (0, 0), (- 1, - 1), 0),
            ('TOPPADDING', (0, 0), (- 1, - 1), 0),
            ('BOTTOMPADDING', (0, 0), (- 1, - 1), 0),
            ]
        for i, entry in enumerate(_tempEntries):
            level, text, pageNum = entry[:3]
            leftColStyle = self.levelStyles[level]
            if i:  # Not for first element
                tableStyle.append((
                    'TOPPADDING',
                    (0, i), (- 1, i),
                    max(lastMargin, leftColStyle.spaceBefore)))
            # print leftColStyle.leftIndent
            lastMargin = leftColStyle.spaceAfter
            #right col style is right aligned
            rightColStyle = ParagraphStyle(name='leftColLevel%d' % level,
                                           parent=leftColStyle,
                                           leftIndent=0,
                                           alignment=TA_RIGHT)
            leftPara = Paragraph(text, leftColStyle)
            rightPara = Paragraph(str(pageNum), rightColStyle)
            tableData.append([leftPara, rightPara])

        self._table = Table(
            tableData,
            colWidths=widths,
            style=TableStyle(tableStyle))

        self.width, self.height = self._table.wrapOn(self.canv, availWidth, availHeight)
        return self.width, self.height


class PmlRightPageBreak(CondPageBreak):

    def __init__(self):
        pass

    def wrap(self, availWidth, availHeight):
        if not self.canv.getPageNumber() % 2:
            self.width = availWidth
            self.height = availHeight
            return availWidth, availHeight
        self.width = self.height = 0
        return 0, 0


class PmlLeftPageBreak(CondPageBreak):

    def __init__(self):
        pass

    def wrap(self, availWidth, availHeight):
        if self.canv.getPageNumber() % 2:
            self.width = availWidth
            self.height = availHeight
            return availWidth, availHeight
        self.width = self.height = 0
        return 0, 0

# --- Pdf Form


class PmlInput(Flowable):

    def __init__(self, name, type="text", width=10, height=10, default="", options=[]):
        self.width = width
        self.height = height
        self.type = type
        self.name = name
        self.default = default
        self.options = options

    def wrap(self, *args):
        return (self.width, self.height)

    def draw(self):
        c = self.canv

        c.saveState()
        c.setFont("Helvetica", 10)
        if self.type == "text":
            pdfform.textFieldRelative(c, self.name, 0, 0, self.width, self.height)
            c.rect(0, 0, self.width, self.height)
        elif self.type == "radio":
            #pdfform.buttonFieldRelative(c, "field2", "Yes", 0, 0)
            c.rect(0, 0, self.width, self.height)
        elif self.type == "checkbox":
            if self.default:
                pdfform.buttonFieldRelative(c, self.name, "Yes", 0, 0)
            else:
                pdfform.buttonFieldRelative(c, self.name, "Off", 0, 0)
            # pdfform.buttonFieldRelative(c, self.name, "Yes" if self.default else "Off", 0, 0)
            c.rect(0, 0, self.width, self.height)
        elif self.type == "select":
            pdfform.selectFieldRelative(c, self.name, self.default, self.options, 0, 0, self.width, self.height)
            c.rect(0, 0, self.width, self.height)

        c.restoreState()

        '''
        canvas.setLineWidth(6)
        canvas.setFillColor(self.fillcolor)
        canvas.setStrokeColor(self.strokecolor)
        canvas.translate(self.xoffset+self.size,0)
        canvas.rotate(90)
        canvas.scale(self.scale, self.scale)
        hand(canvas, debug=0, fill=1)
        '''

"""
# --- Flowable example

def hand(canvas, debug=1, fill=0):
    (startx, starty) = (0, 0)
    curves = [
        (0, 2), (0, 4), (0, 8), # back of hand
        (5, 8), (7, 10), (7, 14),
        (10, 14), (10, 13), (7.5, 8), # thumb
        (13, 8), (14, 8), (17, 8),
        (19, 8), (19, 6), (17, 6),
        (15, 6), (13, 6), (11, 6), # index, pointing
        (12, 6), (13, 6), (14, 6),
        (16, 6), (16, 4), (14, 4),
        (13, 4), (12, 4), (11, 4), # middle
        (11.5, 4), (12, 4), (13, 4),
        (15, 4), (15, 2), (13, 2),
        (12.5, 2), (11.5, 2), (11, 2), # ring
        (11.5, 2), (12, 2), (12.5, 2),
        (14, 2), (14, 0), (12.5, 0),
        (10, 0), (8, 0), (6, 0), # pinky, then close
        ]
    from reportlab.lib.units import inch
    if debug: canvas.setLineWidth(6)
    u = inch * 0.2
    p = canvas.beginPath()
    p.moveTo(startx, starty)
    ccopy = list(curves)
    while ccopy:
        [(x1, y1), (x2, y2), (x3, y3)] = ccopy[:3]
        del ccopy[:3]
        p.curveTo(x1 * u, y1 * u, x2 * u, y2 * u, x3 * u, y3 * u)
    p.close()
    canvas.drawPath(p, fill=fill)
    if debug:
        from reportlab.lib.colors import red, green
        (lastx, lasty) = (startx, starty)
        ccopy = list(curves)
        while ccopy:
            [(x1, y1), (x2, y2), (x3, y3)] = ccopy[:3]
            del ccopy[:3]
            canvas.setStrokeColor(red)
            canvas.line(lastx * u, lasty * u, x1 * u, y1 * u)
            canvas.setStrokeColor(green)
            canvas.line(x2 * u, y2 * u, x3 * u, y3 * u)
            (lastx, lasty) = (x3, y3)

from reportlab.lib.colors import tan, green

class HandAnnotation(Flowable):

    '''A hand flowable.'''

    def __init__(self, xoffset=0, size=None, fillcolor=tan, strokecolor=green):
        from reportlab.lib.units import inch
        if size is None: size = 4 * inch
        self.fillcolor, self.strokecolor = fillcolor, strokecolor
        self.xoffset = xoffset
        self.size = size
        # normal size is 4 inches
        self.scale = size / (4.0 * inch)

    def wrap(self, *args):
        return (self.xoffset, self.size)

    def draw(self):
        canvas = self.canv
        canvas.setLineWidth(6)
        canvas.setFillColor(self.fillcolor)
        canvas.setStrokeColor(self.strokecolor)
        canvas.translate(self.xoffset + self.size, 0)
        canvas.rotate(90)
        canvas.scale(self.scale, self.scale)
        hand(canvas, debug=0, fill=1)
"""
